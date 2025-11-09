from fastapi import FastAPI, HTTPException, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, OAuth2PasswordBearer
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import httpx
import os
from datetime import datetime, timedelta
import jwt
from passlib.context import CryptContext
from dotenv import load_dotenv
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import base64
import json
import asyncio

load_dotenv()

templates = Jinja2Templates(directory="templates")

# FastAPI App
app = FastAPI(
    title="CodeAtEase API",
    description="AI-powered code editor with GitHub integration",
    version="1.0.0"
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration - CRITICAL: Use environment variables for production
SECRET_KEY = os.environ.get("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440

# Get base URL from environment or request
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
# Dynamic redirect URI based on BASE_URL
GITHUB_REDIRECT_URI = os.getenv("GITHUB_REDIRECT_URI", f"{BASE_URL}/auth/github/callback")

#Huggingface Token
HF_TOKEN = os.getenv("HF_TOKEN")

# Security
security = HTTPBearer()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# In-memory storage
users_db: Dict[int, Dict] = {}
tokens_db: Dict[str, int] = {}
chat_history: Dict[str, List[Dict]] = {}

# Helper function to get base URL from request
def get_base_url(request: Request) -> str:
    """Get base URL from request or environment"""
    if BASE_URL and BASE_URL != "http://localhost:8000":
        return BASE_URL
    
    # Construct from request
    scheme = request.url.scheme
    host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}"

# ==================== MODELS ====================

class User(BaseModel):
    id: int
    username: str
    name: str
    email: Optional[str] = None
    avatar: str

class ChatMessage(BaseModel):
    role: str
    content: str
    timestamp: str
    fileContext: Optional[Dict[str, Any]] = None

class AnalyzeRequest(BaseModel):
    prompt: str
    selectedCode: Optional[str] = ""
    currentFile: Optional[Dict[str, Any]] = {}
    repository: Optional[List[Dict[str, Any]]] = []
    conversationHistory: Optional[List[Dict[str, Any]]] = []

class UpdateFileRequest(BaseModel):
    owner: str
    repo: str
    path: str
    content: str
    message: str
    sha: str
    branch: Optional[str] = "main"

class CreateFileRequest(BaseModel):
    owner: str
    repo: str
    path: str
    content: str
    message: str
    branch: Optional[str] = "main"

class DeleteFileRequest(BaseModel):
    owner: str
    repo: str
    path: str
    message: str
    sha: str
    branch: Optional[str] = "main"

class RenameFileRequest(BaseModel):
    owner: str
    repo: str
    oldPath: str
    newPath: str
    message: str
    sha: str
    branch: Optional[str] = "main"

class PushChangesRequest(BaseModel):
    owner: str
    repo: str
    changes: List[Dict[str, Any]]
    commitMessage: str
    branch: Optional[str] = "main"

# ==================== AUTHENTICATION ====================

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    if "sub" in to_encode and isinstance(to_encode["sub"], int):
        to_encode["sub"] = str(to_encode["sub"])
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(status_code=401, detail="Token missing")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id_str = payload.get("sub")
        if user_id_str is None:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        user_id = int(user_id_str)
        if user_id not in users_db:
            raise HTTPException(status_code=401, detail="User not found")
        return users_db[user_id]
    except jwt.DecodeError:
        raise HTTPException(status_code=401, detail="Token decode error")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid user ID format")

# ==================== TEMPLATE ROUTES ====================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/repo.html", response_class=HTMLResponse)
async def repo_page(request: Request):
    return templates.TemplateResponse("repo.html", {"request": request})

@app.get("/aipage.html", response_class=HTMLResponse)
async def ai_page(request: Request):
    return templates.TemplateResponse("aipage.html", {"request": request})

# API endpoint to get configuration
@app.get("/api/config")
async def get_config(request: Request):
    """Return client configuration including base URL"""
    base_url = get_base_url(request)
    return {
        "baseUrl": base_url,
        "environment": "production" if "render" in base_url or "railway" in base_url else "development"
    }

# ==================== AUTH ROUTES ====================

@app.get("/auth/github")
async def github_login(request: Request):
    """Redirect to GitHub OAuth"""
    base_url = get_base_url(request)
    redirect_uri = f"{base_url}/auth/github/callback"
    
    auth_url = (
        f"https://github.com/login/oauth/authorize?"
        f"client_id={GITHUB_CLIENT_ID}&"
        f"redirect_uri={redirect_uri}&"
        f"scope=repo,user"
    )
    return RedirectResponse(auth_url)

@app.get("/auth/github/callback")
async def github_callback(code: str, request: Request):
    """Handle GitHub OAuth callback"""
    if not code:
        raise HTTPException(status_code=400, detail="No code provided")
    
    base_url = get_base_url(request)
    redirect_uri = f"{base_url}/auth/github/callback"
    
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": redirect_uri
            }
        )
        
        token_data = token_response.json()
        github_access_token = token_data.get("access_token")
        
        if not github_access_token:
            raise HTTPException(status_code=400, detail="Failed to get access token")
        
        user_response = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"token {github_access_token}",
                "Accept": "application/json"
            }
        )
        
        user_data = user_response.json()
        user_id = user_data["id"]
        
        users_db[user_id] = {
            "id": user_id,
            "username": user_data["login"],
            "name": user_data.get("name", user_data["login"]),
            "email": user_data.get("email", ""),
            "avatar": user_data["login"][:2].upper(),
            "github_token": github_access_token,
            "created_at": datetime.now().isoformat()
        }
        
        jwt_token = create_access_token(data={"sub": user_id})
        tokens_db[jwt_token] = user_id
        
        # Use base_url for redirect
        redirect_url = f"{base_url}/repo.html?access_token={jwt_token}"
        return RedirectResponse(redirect_url)

@app.get("/auth/user", response_model=User)
async def get_current_user_info(current_user: dict = Depends(get_current_user)):
    """Get current authenticated user"""
    return User(
        id=current_user.get("id"),
        username=current_user.get("username", "unknown"),
        name=current_user.get("name", "Unknown User"),
        email=current_user.get("email", ""),
        avatar=current_user.get("avatar") or current_user.get("avatar_url", "??")
    )

@app.post("/auth/logout")
async def logout(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Logout user"""
    token = credentials.credentials
    if token in tokens_db:
        user_id = tokens_db[token]
        if str(user_id) in chat_history:
            del chat_history[str(user_id)]
        del tokens_db[token]
    return {"message": "Logged out successfully"}

# ==================== REPOSITORY ROUTES ====================
# [Keep all your existing repository routes - they're fine]

@app.get("/api/repositories")
async def get_repositories(current_user: dict = Depends(get_current_user)):
    """Get all repositories for authenticated user"""
    github_token = current_user["github_token"]
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            all_repos = []
            page = 1
            per_page = 100
            
            while True:
                response = await client.get(
                    "https://api.github.com/user/repos",
                    headers={
                        "Authorization": f"token {github_token}",
                        "Accept": "application/vnd.github.v3+json"
                    },
                    params={
                        "per_page": per_page,
                        "page": page,
                        "sort": "updated",
                        "affiliation": "owner,collaborator,organization_member"
                    }
                )
                
                if response.status_code != 200:
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"GitHub API error: {response.text}"
                    )
                
                repos = response.json()
                
                if not repos:
                    break
                
                all_repos.extend(repos)
                
                if len(repos) < per_page:
                    break
                
                page += 1
            
            repositories = [{
                "id": repo["id"],
                "name": repo["name"],
                "full_name": repo["full_name"],
                "owner": repo["owner"]["login"],
                "description": repo.get("description", ""),
                "private": repo["private"],
                "url": repo["html_url"],
                "clone_url": repo["clone_url"],
                "default_branch": repo.get("default_branch", "main"),
                "language": repo.get("language", ""),
                "stargazers_count": repo.get("stargazers_count", 0),
                "forks_count": repo.get("forks_count", 0),
                "updated_at": repo["updated_at"],
                "created_at": repo["created_at"],
                "size": repo.get("size", 0)
            } for repo in all_repos]
            
            return {"repositories": repositories, "total": len(repositories)}
        
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="GitHub API timeout")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to fetch repositories: {str(e)}")
        
@app.get("/api/repository/tree/{owner}/{repo}")
async def get_repository_tree(owner: str, repo: str, current_user: dict = Depends(get_current_user)):
    github_token = current_user["github_token"]
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            repo_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}",
                headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"}
            )
            if repo_response.status_code != 200:
                raise HTTPException(status_code=404, detail="Repository not found")
            repo_data = repo_response.json()
            default_branch = repo_data.get("default_branch", "main")
            tree_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/git/trees/{default_branch}?recursive=1",
                headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"}
            )
            if tree_response.status_code != 200:
                raise HTTPException(status_code=404, detail="Failed to fetch repository tree")
            tree_data = tree_response.json()
            file_tree = build_tree_structure(tree_data["tree"])
            return {"owner": owner, "repo": repo, "default_branch": default_branch, "tree": file_tree}
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="GitHub API timeout")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to fetch repository tree: {str(e)}")

@app.get("/api/repository/file/{owner}/{repo}")
async def get_file_content(
    owner: str,
    repo: str,
    path: str,
    current_user: dict = Depends(get_current_user)
):
    """Get file content from repository"""
    github_token = current_user["github_token"]
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
                headers={
                    "Authorization": f"token {github_token}",
                    "Accept": "application/vnd.github.v3+json"
                }
            )
            
            if response.status_code != 200:
                raise HTTPException(status_code=404, detail="File not found")
            
            file_data = response.json()
            
            try:
                content = base64.b64decode(file_data["content"]).decode("utf-8")
            except:
                content = "[Binary file - cannot display]"
            
            return {
                "path": file_data["path"],
                "name": file_data["name"],
                "content": content,
                "sha": file_data["sha"],
                "size": file_data["size"]
            }
            
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to fetch file: {str(e)}")

@app.put("/api/repository/file/update")
async def update_file(
    request: UpdateFileRequest,
    current_user: dict = Depends(get_current_user)
):
    """Update file content in repository"""
    github_token = current_user["github_token"]
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            encoded_content = base64.b64encode(request.content.encode("utf-8")).decode("utf-8")
            
            response = await client.put(
                f"https://api.github.com/repos/{request.owner}/{request.repo}/contents/{request.path}",
                headers={
                    "Authorization": f"token {github_token}",
                    "Accept": "application/vnd.github.v3+json"
                },
                json={
                    "message": request.message,
                    "content": encoded_content,
                    "sha": request.sha,
                    "branch": request.branch
                }
            )
            
            if response.status_code not in [200, 201]:
                raise HTTPException(status_code=400, detail=f"Failed to update file: {response.text}")
            
            result = response.json()
            
            return {
                "message": "File updated successfully",
                "sha": result["content"]["sha"],
                "commit": result["commit"]["sha"]
            }
            
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to update file: {str(e)}")

@app.post("/api/repository/file/create")
async def create_file(
    request: CreateFileRequest,
    current_user: dict = Depends(get_current_user)
):
    """Create new file in repository"""
    github_token = current_user["github_token"]
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            encoded_content = base64.b64encode(request.content.encode("utf-8")).decode("utf-8")
            
            response = await client.put(
                f"https://api.github.com/repos/{request.owner}/{request.repo}/contents/{request.path}",
                headers={
                    "Authorization": f"token {github_token}",
                    "Accept": "application/vnd.github.v3+json"
                },
                json={
                    "message": request.message,
                    "content": encoded_content,
                    "branch": request.branch
                }
            )
            
            if response.status_code not in [200, 201]:
                raise HTTPException(status_code=400, detail=f"Failed to create file: {response.text}")
            
            result = response.json()
            
            return {
                "message": "File created successfully",
                "sha": result["content"]["sha"],
                "commit": result["commit"]["sha"],
                "path": result["content"]["path"]
            }
            
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create file: {str(e)}")

@app.delete("/api/repository/file/delete")
async def delete_file(
    request: DeleteFileRequest,
    current_user: dict = Depends(get_current_user)
):
    """Delete file from repository"""
    github_token = current_user["github_token"]
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.delete(
                f"https://api.github.com/repos/{request.owner}/{request.repo}/contents/{request.path}",
                headers={
                    "Authorization": f"token {github_token}",
                    "Accept": "application/vnd.github.v3+json"
                },
                json={
                    "message": request.message,
                    "sha": request.sha,
                    "branch": request.branch
                }
            )
            
            if response.status_code not in [200, 204]:
                raise HTTPException(status_code=400, detail=f"Failed to delete file: {response.text}")
            
            return {
                "message": "File deleted successfully",
                "path": request.path
            }
            
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")

@app.put("/api/repository/file/rename")
async def rename_file(
    request: RenameFileRequest,
    current_user: dict = Depends(get_current_user)
):
    """Rename file in repository"""
    github_token = current_user["github_token"]
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # First, get the old file content
            get_response = await client.get(
                f"https://api.github.com/repos/{request.owner}/{request.repo}/contents/{request.oldPath}",
                headers={
                    "Authorization": f"token {github_token}",
                    "Accept": "application/vnd.github.v3+json"
                }
            )
            
            if get_response.status_code != 200:
                raise HTTPException(status_code=404, detail="File not found")
            
            file_data = get_response.json()
            content = file_data["content"]
            
            # Create file with new name
            create_response = await client.put(
                f"https://api.github.com/repos/{request.owner}/{request.repo}/contents/{request.newPath}",
                headers={
                    "Authorization": f"token {github_token}",
                    "Accept": "application/vnd.github.v3+json"
                },
                json={
                    "message": request.message,
                    "content": content,
                    "branch": request.branch
                }
            )
            
            if create_response.status_code not in [200, 201]:
                raise HTTPException(status_code=400, detail=f"Failed to create renamed file: {create_response.text}")
            
            # Delete old file
            delete_response = await client.delete(
                f"https://api.github.com/repos/{request.owner}/{request.repo}/contents/{request.oldPath}",
                headers={
                    "Authorization": f"token {github_token}",
                    "Accept": "application/vnd.github.v3+json"
                },
                json={
                    "message": request.message,
                    "sha": request.sha,
                    "branch": request.branch
                }
            )
            
            if delete_response.status_code not in [200, 204]:
                raise HTTPException(status_code=400, detail=f"Failed to delete old file: {delete_response.text}")
            
            result = create_response.json()
            
            return {
                "message": "File renamed successfully",
                "oldPath": request.oldPath,
                "newPath": request.newPath,
                "sha": result["content"]["sha"]
            }
            
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to rename file: {str(e)}")

@app.post("/api/repository/push")
async def push_changes(
    request: PushChangesRequest,
    current_user: dict = Depends(get_current_user)
):
    """Push multiple file changes to repository"""
    github_token = current_user["github_token"]
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            results = []
            
            for change in request.changes:
                encoded_content = base64.b64encode(change["content"].encode("utf-8")).decode("utf-8")
                
                response = await client.put(
                    f"https://api.github.com/repos/{request.owner}/{request.repo}/contents/{change['path']}",
                    headers={
                        "Authorization": f"token {github_token}",
                        "Accept": "application/vnd.github.v3+json"
                    },
                    json={
                        "message": request.commitMessage,
                        "content": encoded_content,
                        "sha": change["sha"],
                        "branch": request.branch
                    }
                )
                
                if response.status_code in [200, 201]:
                    result = response.json()
                    results.append({
                        "path": change["path"],
                        "status": "success",
                        "sha": result["content"]["sha"]
                    })
                else:
                    results.append({
                        "path": change["path"],
                        "status": "failed",
                        "error": response.text
                    })
            
            return {
                "message": "Push completed",
                "results": results,
                "totalFiles": len(request.changes),
                "successCount": len([r for r in results if r["status"] == "success"])
            }
            
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to push changes: {str(e)}")

# ==================== CHAT & AI ANALYSIS ====================

@app.post("/api/chat")
async def chat_with_ai(
    request: AnalyzeRequest,
    current_user: dict = Depends(get_current_user)
):
    """Chat with AI assistant using DeepSeek via Hugging Face"""
    if not request.prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")
    
    user_id = str(current_user["id"])
    
    if user_id not in chat_history:
        chat_history[user_id] = []
    
    # Build context-aware prompt
    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(request, chat_history[user_id])
    
    try:
        # Call Hugging Face API with DeepSeek
        if HF_TOKEN:
            print(f"[CHAT] User {user_id} asking: {request.prompt[:100]}")
            response_text = await call_deepseek_api(system_prompt, user_prompt, chat_history[user_id])
            print(f"[CHAT] AI responded: {response_text[:100]}...")
        else:
            print("[CHAT] No HF_TOKEN, using mock response")
            response_text = generate_mock_response(request)
        
        # Store messages in history
        user_message = {
            "role": "user",
            "content": request.prompt,
            "timestamp": datetime.now().isoformat(),
            "fileContext": {
                "path": request.currentFile.get("path") if request.currentFile else None,
                "hasSelection": bool(request.selectedCode)
            }
        }
        chat_history[user_id].append(user_message)
        
        assistant_message = {
            "role": "assistant",
            "content": response_text,
            "timestamp": datetime.now().isoformat()
        }
        chat_history[user_id].append(assistant_message)
        
        # Keep only last 20 messages
        if len(chat_history[user_id]) > 20:
            chat_history[user_id] = chat_history[user_id][-20:]
        
        return {
            "response": response_text,
            "conversationHistory": chat_history[user_id]
        }
    
    except Exception as e:
        print(f"[CHAT] AI Error: {str(e)}")
        print(f"[CHAT] Falling back to mock response")
        response_text = generate_mock_response(request)
        return {
            "response": response_text,
            "conversationHistory": chat_history.get(user_id, []),
            "error": str(e),
            "fallback": True
        }

async def call_deepseek_api(system_prompt: str, user_prompt: str, history: List[Dict]) -> str:
    """Call AI model via Hugging Face Router API"""
    
    # Build messages for chat completion
    messages = [
        {"role": "system", "content": system_prompt}
    ]
    
    # Add conversation history
    recent_history = history[-4:] if len(history) > 4 else history
    for msg in recent_history:
        messages.append({
            "role": msg["role"],
            "content": msg["content"][:500]  # Limit history length
        })
    
    # Add current user prompt
    messages.append({"role": "user", "content": user_prompt})
    
    # Call Hugging Face Router API (new endpoint)
    async with httpx.AsyncClient(timeout=90.0) as client:
        try:
            print(f"[AI] Calling Hugging Face Router API...")
            
            response = await client.post(
                "https://router.huggingface.co/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {HF_TOKEN}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "Qwen/Qwen2.5-Coder-32B-Instruct",
                    "messages": messages,
                    "max_tokens": 2000,
                    "temperature": 0.7,
                    "top_p": 0.95,
                    "stream": False
                }
            )
            
            print(f"[AI] Response status: {response.status_code}")
            
            if response.status_code == 503:
                # Model is loading, wait and retry
                print("[AI] Model is loading, retrying in 10 seconds...")
                await asyncio.sleep(10)
                
                response = await client.post(
                    "https://router.huggingface.co/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {HF_TOKEN}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "Qwen/Qwen2.5-Coder-32B-Instruct",
                        "messages": messages,
                        "max_tokens": 2000,
                        "temperature": 0.7,
                        "top_p": 0.95,
                        "stream": False
                    }
                )
            
            if response.status_code != 200:
                error_text = response.text
                print(f"[AI] API Error: {response.status_code} - {error_text}")
                raise Exception(f"API returned status {response.status_code}: {error_text}")
            
            result = response.json()
            print(f"[AI] Response received: {str(result)[:200]}...")
            
            # Extract response from OpenAI-compatible format
            if "choices" in result and len(result["choices"]) > 0:
                content = result["choices"][0]["message"]["content"]
                return content.strip()
            
            raise Exception(f"Unexpected API response format: {result}")
                
        except httpx.TimeoutException:
            print("[AI] Request timed out")
            raise Exception("API request timed out")
        except Exception as e:
            print(f"[AI] Error: {str(e)}")
            raise

def build_system_prompt() -> str:
    """Build system prompt for the AI"""
    return """You are CatAI, an expert code assistant. You MUST provide complete, working code solutions.

CRITICAL RULES:
1. When asked to add/modify code, show the COMPLETE updated file content
2. NEVER give generic advice - always show actual code
3. Format code in markdown blocks with language: ```python or ```yaml etc.
4. Be concise but complete
5. When asked to create files or folders, provide exact file paths and complete content
6. For file operations, always specify clear file paths

Example response format:

Here's the updated requirements.txt:

```txt
fastapi==0.104.1
uvicorn==0.24.0
gunicorn==21.2.0
httpx==0.25.1
```

I added gunicorn version 21.2.0 to the requirements."""

def build_user_prompt(request: AnalyzeRequest, history: List[Dict]) -> str:
    """Build user prompt with context"""
    
    prompt_parts = []
    
    # Add current file context
    if request.currentFile and request.currentFile.get('path'):
        prompt_parts.append(f"**Current File:** `{request.currentFile.get('path')}`")
        
        if request.currentFile.get('content'):
            content_preview = request.currentFile.get('content')[:1500]
            prompt_parts.append(f"\n**File Content:**\n```\n{content_preview}\n```")
    
    # Add selected code if exists
    if request.selectedCode:
        prompt_parts.append(f"\n**Selected Code:**\n```\n{request.selectedCode}\n```")
    
    # Add user's question
    prompt_parts.append(f"\n**User Request:** {request.prompt}")
    
    return "\n".join(prompt_parts)

@app.get("/api/chat/history")
async def get_chat_history(current_user: dict = Depends(get_current_user)):
    """Get chat history for current user"""
    user_id = str(current_user["id"])
    return {"history": chat_history.get(user_id, [])}

@app.delete("/api/chat/history")
async def clear_chat_history(current_user: dict = Depends(get_current_user)):
    """Clear chat history for current user"""
    user_id = str(current_user["id"])
    if user_id in chat_history:
        chat_history[user_id] = []
    return {"message": "Chat history cleared"}

def generate_mock_response(request: AnalyzeRequest) -> str:
    """Generate mock AI response for testing"""
    response = f"I understand you want help with: '{request.prompt}'\n\n"
    
    if request.currentFile and request.currentFile.get('path'):
        response += f"Looking at file: `{request.currentFile.get('path')}`\n\n"
    
    if request.selectedCode:
        response += f"Analyzing your selected code...\n\n"
    
    response += """Here's my analysis:

✓ **Code Structure:** Well organized
✓ **Best Practices:** Consider improvements below
✓ **Performance:** No major bottlenecks detected

**Recommendations:**
1. Add error handling for edge cases
2. Include input validation
3. Add unit tests for critical functions
4. Consider adding documentation

Would you like me to help implement any of these improvements?"""
    
    return response

def build_tree_structure(items: List[Dict]) -> List[Dict]:
    tree = []
    path_dict = {}
    items_sorted = sorted(items, key=lambda x: (x['path'].count('/'), x['path']))
    for item in items_sorted:
        path = item['path']
        parts = path.split('/')
        node = {"name": parts[-1], "path": path, "type": "folder" if item['type'] == 'tree' else "file"}
        if item['type'] == 'tree':
            node["children"] = []
            node["expanded"] = False
        path_dict[path] = node
        if len(parts) == 1:
            tree.append(node)
        else:
            parent_path = '/'.join(parts[:-1])
            if parent_path in path_dict:
                if "children" not in path_dict[parent_path]:
                    path_dict[parent_path]["children"] = []
                path_dict[parent_path]["children"].append(node)
    return tree

# ==================== HEALTH CHECK ====================

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "ai_available": HF_TOKEN is not None
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)