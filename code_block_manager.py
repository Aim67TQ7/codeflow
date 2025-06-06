#!/usr/bin/env python3
"""
Code Block Manager - Full Application
- Web interface for adding/managing code blocks
- FastAPI backend for search and retrieval
- Connects to your Railway PostgreSQL database
"""

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field

import asyncpg
from fastapi import FastAPI, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import uvicorn

# Load environment variables
load_dotenv()

# FastAPI app
app = FastAPI(title="Code Block Manager", version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database connection
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable not set")

# Global database pool
db_pool: Optional[asyncpg.Pool] = None

@dataclass
class CodeBlock:
    id: Optional[str] = None
    hash: str = ""
    code: str = ""
    description: str = ""
    language: str = ""
    tags: List[str] = field(default_factory=list)
    usage_count: int = 0
    success_rate: float = 1.0
    created_at: Optional[datetime] = None

# Pydantic models for API
class CodeBlockCreate(BaseModel):
    code: str
    description: str
    language: str
    tags: List[str] = []

class CodeBlockSearch(BaseModel):
    query: str
    language: Optional[str] = None
    limit: int = 10

class CodeBlockResponse(BaseModel):
    id: str
    hash: str
    code: str
    description: str
    language: str
    tags: List[str]
    usage_count: int
    success_rate: float
    created_at: str

# Database functions
async def init_db():
    """Initialize database connection"""
    global db_pool
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        command_timeout=60
    )

async def close_db():
    """Close database connection"""
    global db_pool
    if db_pool:
        await db_pool.close()

def detect_language(code: str) -> str:
    """Detect programming language from code"""
    code_lower = code.lower().strip()
    
    # TypeScript/JavaScript
    if any(keyword in code for keyword in ['import {', 'export function', 'interface ', ': string', ': number']):
        if 'interface ' in code or ': string' in code or ': number' in code:
            return 'typescript'
        return 'javascript'
    
    # Python
    if any(keyword in code for keyword in ['def ', 'import ', 'from ', 'class ', '__init__']):
        return 'python'
    
    # SQL
    if any(keyword in code_lower for keyword in ['select ', 'insert ', 'update ', 'delete ', 'create table']):
        return 'sql'
    
    # CSS
    if '{' in code and '}' in code and (':' in code) and any(prop in code_lower for prop in ['color:', 'margin:', 'padding:', 'display:']):
        return 'css'
    
    # HTML
    if '<' in code and '>' in code and any(tag in code_lower for tag in ['<div', '<span', '<html', '<body']):
        return 'html'
    
    # Java
    if any(keyword in code for keyword in ['public class', 'private ', 'public static void main']):
        return 'java'
    
    # Go
    if any(keyword in code for keyword in ['package main', 'func ', 'import (']):
        return 'go'
    
    # Rust
    if any(keyword in code for keyword in ['fn ', 'let mut', 'use std::']):
        return 'rust'
    
    return 'unknown'

def extract_tags(code: str, description: str) -> List[str]:
    """Extract relevant tags from code and description"""
    tags = set()
    
    # Common programming terms
    programming_terms = {
        'api', 'rest', 'database', 'sql', 'react', 'component', 'function',
        'class', 'authentication', 'auth', 'login', 'user', 'crud', 'form',
        'validation', 'email', 'password', 'dashboard', 'admin', 'frontend',
        'backend', 'server', 'client', 'http', 'json', 'xml', 'csv',
        'file', 'upload', 'download', 'image', 'video', 'search', 'filter',
        'sort', 'pagination', 'chart', 'graph', 'table', 'list', 'menu',
        'navbar', 'sidebar', 'modal', 'popup', 'notification', 'alert',
        'formatter', 'parser', 'validator', 'mapper', 'helper', 'utility'
    }
    
    # Extract from description
    desc_words = re.findall(r'\b\w+\b', description.lower())
    for word in desc_words:
        if word in programming_terms:
            tags.add(word)
    
    # Extract from code
    code_words = re.findall(r'\b\w+\b', code.lower())
    for word in code_words:
        if word in programming_terms:
            tags.add(word)
    
    # Language-specific patterns
    if 'function' in code.lower() or 'def ' in code:
        tags.add('function')
    
    if 'class ' in code:
        tags.add('class')
    
    if 'async' in code or 'await' in code:
        tags.add('async')
    
    if 'export' in code:
        tags.add('module')
    
    return list(tags)[:10]  # Limit to 10 tags

# Database operations
async def store_code_block(block: CodeBlockCreate) -> str:
    """Store a new code block"""
    # Generate hash
    code_hash = hashlib.md5(block.code.encode()).hexdigest()
    
    # Auto-detect language if not provided
    if not block.language or block.language == 'auto':
        block.language = detect_language(block.code)
    
    # Auto-extract tags if not provided
    if not block.tags:
        block.tags = extract_tags(block.code, block.description)
    
    async with db_pool.acquire() as conn:
        # Check if block already exists
        existing = await conn.fetchrow(
            "SELECT id FROM code_blocks WHERE hash = $1",
            code_hash
        )
        
        if existing:
            # Update usage count
            await conn.execute(
                "UPDATE code_blocks SET usage_count = usage_count + 1 WHERE hash = $1",
                code_hash
            )
            return str(existing['id'])
        
        # Insert new block
        block_id = await conn.fetchval("""
            INSERT INTO code_blocks (hash, code, description, language, tags, usage_count, success_rate)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
        """, code_hash, block.code, block.description, block.language, 
            block.tags, 0, 1.0)
        
        return str(block_id)

async def search_code_blocks(query: str, language: Optional[str] = None, limit: int = 10) -> List[CodeBlockResponse]:
    """Search for code blocks"""
    search_conditions = []
    params = []
    param_count = 1
    
    # Text search
    if query:
        search_conditions.append(f"""
            (description ILIKE ${param_count} OR 
             code ILIKE ${param_count} OR 
             ${param_count} = ANY(tags))
        """)
        params.append(f"%{query}%")
        param_count += 1
    
    # Language filter
    if language:
        search_conditions.append(f"language = ${param_count}")
        params.append(language)
        param_count += 1
    
    where_clause = "WHERE " + " AND ".join(search_conditions) if search_conditions else ""
    
    sql = f"""
        SELECT id, hash, code, description, language, tags, usage_count, 
               success_rate, created_at
        FROM code_blocks
        {where_clause}
        ORDER BY usage_count DESC, success_rate DESC
        LIMIT ${param_count}
    """
    params.append(limit)
    
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    
    blocks = []
    for row in rows:
        block = CodeBlockResponse(
            id=str(row['id']),
            hash=row['hash'],
            code=row['code'],
            description=row['description'],
            language=row['language'],
            tags=list(row['tags'] or []),
            usage_count=row['usage_count'],
            success_rate=float(row['success_rate']),
            created_at=row['created_at'].isoformat()
        )
        blocks.append(block)
    
    return blocks

async def get_all_blocks(limit: int = 50) -> List[CodeBlockResponse]:
    """Get all code blocks"""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, hash, code, description, language, tags, usage_count, 
                   success_rate, created_at
            FROM code_blocks
            ORDER BY created_at DESC
            LIMIT $1
        """, limit)
    
    blocks = []
    for row in rows:
        block = CodeBlockResponse(
            id=str(row['id']),
            hash=row['hash'],
            code=row['code'],
            description=row['description'],
            language=row['language'],
            tags=list(row['tags'] or []),
            usage_count=row['usage_count'],
            success_rate=float(row['success_rate']),
            created_at=row['created_at'].isoformat()
        )
        blocks.append(block)
    
    return blocks

# Web interface HTML
def get_html_interface():
    """Generate the HTML interface"""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Code Block Manager</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            background: #f5f5f5; 
            color: #333;
        }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .header { 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
            color: white; 
            padding: 2rem; 
            text-align: center; 
            margin-bottom: 2rem;
            border-radius: 10px;
        }
        .tabs { 
            display: flex; 
            background: white; 
            border-radius: 10px; 
            margin-bottom: 2rem; 
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .tab { 
            flex: 1; 
            padding: 1rem; 
            text-align: center; 
            cursor: pointer; 
            border-bottom: 3px solid transparent;
            transition: all 0.3s ease;
        }
        .tab.active { 
            border-bottom-color: #667eea; 
            background: #f8f9ff;
        }
        .tab:hover { background: #f8f9ff; }
        .content { 
            background: white; 
            padding: 2rem; 
            border-radius: 10px; 
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .form-group { margin-bottom: 1.5rem; }
        .form-group label { 
            display: block; 
            margin-bottom: 0.5rem; 
            font-weight: 600; 
            color: #555;
        }
        .form-group input, .form-group textarea, .form-group select { 
            width: 100%; 
            padding: 0.75rem; 
            border: 2px solid #e1e5e9; 
            border-radius: 8px; 
            font-size: 14px;
            transition: border-color 0.3s ease;
        }
        .form-group input:focus, .form-group textarea:focus, .form-group select:focus { 
            outline: none; 
            border-color: #667eea; 
        }
        .form-group textarea { 
            height: 300px; 
            font-family: 'Courier New', monospace; 
            resize: vertical;
        }
        .btn { 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
            color: white; 
            border: none; 
            padding: 0.75rem 2rem; 
            border-radius: 8px; 
            cursor: pointer; 
            font-size: 16px; 
            font-weight: 600;
            transition: transform 0.2s ease;
        }
        .btn:hover { transform: translateY(-2px); }
        .search-container { 
            display: flex; 
            gap: 1rem; 
            margin-bottom: 2rem; 
            align-items: end;
        }
        .search-container .form-group { flex: 1; margin-bottom: 0; }
        .code-block { 
            background: #f8f9fa; 
            border: 1px solid #e9ecef; 
            border-radius: 8px; 
            margin-bottom: 1rem; 
            overflow: hidden;
        }
        .code-block-header { 
            background: #e9ecef; 
            padding: 1rem; 
            border-bottom: 1px solid #dee2e6;
        }
        .code-block-meta { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            margin-bottom: 0.5rem;
        }
        .code-block-title { 
            font-weight: 600; 
            color: #495057;
        }
        .code-block-stats { 
            font-size: 12px; 
            color: #6c757d;
        }
        .code-block-tags { 
            display: flex; 
            gap: 0.5rem; 
            flex-wrap: wrap;
        }
        .tag { 
            background: #667eea; 
            color: white; 
            padding: 0.25rem 0.5rem; 
            border-radius: 4px; 
            font-size: 12px;
        }
        .code-block-content { 
            padding: 1rem; 
        }
        .code-block-code { 
            background: #2d3748; 
            color: #e2e8f0; 
            padding: 1rem; 
            border-radius: 6px; 
            font-family: 'Courier New', monospace; 
            font-size: 13px; 
            overflow-x: auto; 
            white-space: pre;
        }
        .hidden { display: none; }
        .message { 
            padding: 1rem; 
            border-radius: 8px; 
            margin-bottom: 1rem;
        }
        .message.success { 
            background: #d4edda; 
            border: 1px solid #c3e6cb; 
            color: #155724;
        }
        .message.error { 
            background: #f8d7da; 
            border: 1px solid #f5c6cb; 
            color: #721c24;
        }
        .loading { 
            text-align: center; 
            padding: 2rem; 
            color: #6c757d;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🧱 Code Block Manager</h1>
            <p>Store, search, and reuse proven code blocks</p>
        </div>

        <div class="tabs">
            <div class="tab active" onclick="showTab('add')">➕ Add Code Block</div>
            <div class="tab" onclick="showTab('search')">🔍 Search Blocks</div>
            <div class="tab" onclick="showTab('browse')">📚 Browse All</div>
        </div>

        <!-- Add Code Block Tab -->
        <div id="add-tab" class="content">
            <h2>Add New Code Block</h2>
            <div id="add-message"></div>
            
            <form id="add-form">
                <div class="form-group">
                    <label for="code">Code:</label>
                    <textarea id="code" name="code" placeholder="Paste your code here..." required></textarea>
                </div>
                
                <div class="form-group">
                    <label for="description">Description:</label>
                    <input type="text" id="description" name="description" 
                           placeholder="What does this code do?" required>
                </div>
                
                <div class="form-group">
                    <label for="language">Language:</label>
                    <select id="language" name="language">
                        <option value="auto">Auto-detect</option>
                        <option value="typescript">TypeScript</option>
                        <option value="javascript">JavaScript</option>
                        <option value="python">Python</option>
                        <option value="sql">SQL</option>
                        <option value="html">HTML</option>
                        <option value="css">CSS</option>
                        <option value="java">Java</option>
                        <option value="go">Go</option>
                        <option value="rust">Rust</option>
                        <option value="other">Other</option>
                    </select>
                </div>
                
                <div class="form-group">
                    <label for="tags">Tags (comma-separated):</label>
                    <input type="text" id="tags" name="tags" 
                           placeholder="formatter, api, response, json (auto-generated if empty)">
                </div>
                
                <button type="submit" class="btn">💾 Store Code Block</button>
            </form>
        </div>

        <!-- Search Tab -->
        <div id="search-tab" class="content hidden">
            <h2>Search Code Blocks</h2>
            
            <div class="search-container">
                <div class="form-group">
                    <label for="search-query">Search Query:</label>
                    <input type="text" id="search-query" placeholder="formatter, api, authentication...">
                </div>
                <div class="form-group">
                    <label for="search-language">Language:</label>
                    <select id="search-language">
                        <option value="">All Languages</option>
                        <option value="typescript">TypeScript</option>
                        <option value="javascript">JavaScript</option>
                        <option value="python">Python</option>
                        <option value="sql">SQL</option>
                        <option value="html">HTML</option>
                        <option value="css">CSS</option>
                    </select>
                </div>
                <button onclick="searchBlocks()" class="btn">🔍 Search</button>
            </div>
            
            <div id="search-results"></div>
        </div>

        <!-- Browse Tab -->
        <div id="browse-tab" class="content hidden">
            <h2>Browse All Code Blocks</h2>
            <button onclick="loadAllBlocks()" class="btn">📚 Load All Blocks</button>
            <div id="browse-results"></div>
        </div>
    </div>

    <script>
        function showTab(tabName) {
            // Hide all tabs
            document.querySelectorAll('.content').forEach(el => el.classList.add('hidden'));
            document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
            
            // Show selected tab
            document.getElementById(tabName + '-tab').classList.remove('hidden');
            event.target.classList.add('active');
        }

        function showMessage(elementId, message, type) {
            const el = document.getElementById(elementId);
            el.innerHTML = `<div class="message ${type}">${message}</div>`;
            setTimeout(() => el.innerHTML = '', 5000);
        }

        // Add form submission
        document.getElementById('add-form').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const formData = new FormData(e.target);
            const data = {
                code: formData.get('code'),
                description: formData.get('description'),
                language: formData.get('language'),
                tags: formData.get('tags') ? formData.get('tags').split(',').map(t => t.trim()) : []
            };
            
            try {
                const response = await fetch('/api/blocks', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                
                if (response.ok) {
                    const result = await response.json();
                    showMessage('add-message', `✅ Code block stored successfully! ID: ${result.id}`, 'success');
                    e.target.reset();
                } else {
                    const error = await response.json();
                    showMessage('add-message', `❌ Error: ${error.detail}`, 'error');
                }
            } catch (error) {
                showMessage('add-message', `❌ Network error: ${error.message}`, 'error');
            }
        });

        async function searchBlocks() {
            const query = document.getElementById('search-query').value;
            const language = document.getElementById('search-language').value;
            
            if (!query.trim()) {
                alert('Please enter a search query');
                return;
            }
            
            const resultsEl = document.getElementById('search-results');
            resultsEl.innerHTML = '<div class="loading">Searching...</div>';
            
            try {
                const params = new URLSearchParams({
                    q: query,
                    limit: 20
                });
                if (language) params.append('language', language);
                
                const response = await fetch(`/api/search?${params}`);
                const blocks = await response.json();
                
                displayBlocks(blocks, 'search-results');
            } catch (error) {
                resultsEl.innerHTML = `<div class="message error">❌ Search error: ${error.message}</div>`;
            }
        }

        async function loadAllBlocks() {
            const resultsEl = document.getElementById('browse-results');
            resultsEl.innerHTML = '<div class="loading">Loading all blocks...</div>';
            
            try {
                const response = await fetch('/api/blocks');
                const blocks = await response.json();
                
                displayBlocks(blocks, 'browse-results');
            } catch (error) {
                resultsEl.innerHTML = `<div class="message error">❌ Load error: ${error.message}</div>`;
            }
        }

        function displayBlocks(blocks, containerId) {
            const container = document.getElementById(containerId);
            
            if (!blocks || blocks.length === 0) {
                container.innerHTML = '<div class="message">No code blocks found.</div>';
                return;
            }
            
            const html = blocks.map(block => `
                <div class="code-block">
                    <div class="code-block-header">
                        <div class="code-block-meta">
                            <span class="code-block-title">${escapeHtml(block.description)}</span>
                            <span class="code-block-stats">
                                ${block.language} • Used ${block.usage_count}x • ${(block.success_rate * 100).toFixed(0)}% success
                            </span>
                        </div>
                        <div class="code-block-tags">
                            ${block.tags.map(tag => `<span class="tag">${escapeHtml(tag)}</span>`).join('')}
                        </div>
                    </div>
                    <div class="code-block-content">
                        <div class="code-block-code">${escapeHtml(block.code)}</div>
                    </div>
                </div>
            `).join('');
            
            container.innerHTML = html;
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        // Allow Enter key to search
        document.getElementById('search-query').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                searchBlocks();
            }
        });
    </script>
</body>
</html>
"""

# API Routes
@app.on_event("startup")
async def startup():
    await init_db()

@app.on_event("shutdown")
async def shutdown():
    await close_db()

@app.get("/", response_class=HTMLResponse)
async def get_interface():
    """Serve the web interface"""
    return get_html_interface()

@app.post("/api/blocks")
async def create_block(block: CodeBlockCreate):
    """Create a new code block"""
    try:
        block_id = await store_code_block(block)
        return {"id": block_id, "message": "Code block stored successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/blocks")
async def get_blocks(limit: int = 50):
    """Get all code blocks"""
    try:
        blocks = await get_all_blocks(limit)
        return blocks
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/search")
async def search_blocks_endpoint(q: str, language: Optional[str] = None, limit: int = 10):
    """Search code blocks"""
    try:
        blocks = await search_code_blocks(q, language, limit)
        return blocks
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stats")
async def get_stats():
    """Get system statistics"""
    try:
        async with db_pool.acquire() as conn:
            stats = await conn.fetchrow("""
                SELECT 
                    COUNT(*) as total_blocks,
                    COUNT(DISTINCT language) as languages,
                    AVG(usage_count) as avg_usage,
                    AVG(success_rate) as avg_success_rate
                FROM code_blocks
            """)
            
            top_languages = await conn.fetch("""
                SELECT language, COUNT(*) as count
                FROM code_blocks
                GROUP BY language
                ORDER BY count DESC
                LIMIT 5
            """)
            
            return {
                "total_blocks": stats['total_blocks'],
                "languages": stats['languages'],
                "avg_usage": float(stats['avg_usage'] or 0),
                "avg_success_rate": float(stats['avg_success_rate'] or 0),
                "top_languages": {row['language']: row['count'] for row in top_languages}
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
