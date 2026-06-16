from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import pymysql
import bcrypt
import jwt
from datetime import datetime, timedelta
import base64
from PIL import Image
import io
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from typing import Optional, Dict, List
import json
import traceback
import re

app = FastAPI()
# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



import os
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "voicebridge_secret_2026")
API_KEY = os.getenv("API_KEY")


DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME", "sign_language_db"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor
}

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.rooms: Dict[str, List[str]] = {}

    async def connect(self, websocket: WebSocket, room_id: str, user_id: str):
        await websocket.accept()
        connection_key = f"{room_id}:{user_id}"
        self.active_connections[connection_key] = websocket
        if room_id not in self.rooms:
            self.rooms[room_id] = []
        if user_id not in self.rooms[room_id]:
            self.rooms[room_id].append(user_id)
        print(f"✅ User {user_id} connected to room {room_id}")

    def disconnect(self, room_id: str, user_id: str):
        connection_key = f"{room_id}:{user_id}"
        if connection_key in self.active_connections:
            del self.active_connections[connection_key]
        if room_id in self.rooms and user_id in self.rooms[room_id]:
            self.rooms[room_id].remove(user_id)
        print(f"❌ User {user_id} disconnected from room {room_id}")

    async def send_to_room(self, room_id: str, message: dict, sender_id: str = None):
        if room_id not in self.rooms:
            return
        users_in_room = self.rooms[room_id]
        disconnected = []
        for user_id in users_in_room:
            if sender_id and user_id == sender_id:
                continue
            connection_key = f"{room_id}:{user_id}"
            if connection_key in self.active_connections:
                try:
                    await self.active_connections[connection_key].send_json(message)
                except Exception as e:
                    print(f"❌ Failed to send to {user_id}: {e}")
                    disconnected.append(user_id)
        for user_id in disconnected:
            self.disconnect(room_id, user_id)

manager = ConnectionManager()

# Database helper
def get_db_connection():
    try:
        conn = pymysql.connect(**DB_CONFIG)
        return conn
    except Exception as e:
        print(f"Database connection error: {e}")
        raise HTTPException(status_code=500, detail="Database connection failed")

# Pydantic models
class UserRegister(BaseModel):
    username: str
    password: str
    email: str

class UserLogin(BaseModel):
    username: str
    password: str

class TextToEmojiRequest(BaseModel):
    text: str
    mode: str = "story"  # story | single | sentence | mood

# Initialize LLM
try:
    llm = ChatOpenAI(
        model="openai/gpt-4o-mini",
        api_key=API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )
except Exception as e:
    print(f"LLM initialization warning: {e}")
    llm = None

# Helper functions
def encode_image(image_bytes, max_width=512):
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    w_percent = max_width / float(img.width)
    h_size = int((float(img.height) * float(w_percent)))
    img = img.resize((max_width, h_size))
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG", quality=85)
    img_bytes = buffered.getvalue()
    return base64.b64encode(img_bytes).decode("utf-8")

def create_token(user_id: int, username: str):
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": datetime.utcnow() + timedelta(days=7)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        if 'user_id' not in payload:
            return None
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except Exception as e:
        print(f"Token verification failed: {e}")
        return None

# --- API Routes ---

@app.get("/")
async def root():
    return {"status": "ok", "message": "Sign Language API is running"}

@app.post("/api/register")
async def register(user: UserRegister):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE username = %s", (user.username,))
            if cursor.fetchone():
                raise HTTPException(status_code=400, detail="Username already exists")
            cursor.execute("SELECT id FROM users WHERE email = %s", (user.email,))
            if cursor.fetchone():
                raise HTTPException(status_code=400, detail="Email already exists")
            hashed_password = bcrypt.hashpw(user.password.encode('utf-8'), bcrypt.gensalt())
            cursor.execute(
                "INSERT INTO users (username, password, email, created_at) VALUES (%s, %s, %s, %s)",
                (user.username, hashed_password.decode('utf-8'), user.email, datetime.now())
            )
            conn.commit()
            print(f"✅ User registered: {user.username}")
            return {"message": "User registered successfully"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Registration error: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.post("/api/login")
async def login(user: UserLogin):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE username = %s", (user.username,))
            db_user = cursor.fetchone()
            if not db_user:
                raise HTTPException(status_code=401, detail="Invalid credentials")
            stored_password = db_user['password']
            if isinstance(stored_password, str):
                stored_password = stored_password.encode('utf-8')
            if not bcrypt.checkpw(user.password.encode('utf-8'), stored_password):
                raise HTTPException(status_code=401, detail="Invalid credentials")
            token = create_token(db_user['id'], db_user['username'])
            print(f"✅ User logged in: {user.username} (ID: {db_user['id']})")
            return {"token": token, "user_id": db_user['id'], "username": db_user['username']}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Login error: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.post("/api/rooms/create")
async def create_room(room_name: str = Form(...), token: str = Form(...)):
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO rooms (room_name, creator_id, created_at) VALUES (%s, %s, %s)",
                (room_name, payload['user_id'], datetime.now())
            )
            conn.commit()
            room_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO room_members (room_id, user_id, joined_at) VALUES (%s, %s, %s)",
                (room_id, payload['user_id'], datetime.now())
            )
            conn.commit()
            print(f"✅ Room created: ID={room_id}, Name='{room_name}'")
            return {"room_id": room_id, "room_name": room_name}
    except Exception as e:
        print(f"❌ Create room error: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.post("/api/rooms/join")
async def join_room(room_id: str = Form(...), token: str = Form(...)):
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM rooms WHERE id = %s", (room_id,))
            room_data = cursor.fetchone()
            if not room_data:
                raise HTTPException(status_code=404, detail="Room not found")
            cursor.execute(
                "SELECT * FROM room_members WHERE room_id = %s AND user_id = %s",
                (room_id, payload['user_id'])
            )
            if cursor.fetchone():
                return {"message": "Already in room", "room_id": int(room_id), "room_name": room_data['room_name']}
            cursor.execute(
                "INSERT INTO room_members (room_id, user_id, joined_at) VALUES (%s, %s, %s)",
                (room_id, payload['user_id'], datetime.now())
            )
            conn.commit()
            print(f"✅ User {payload['username']} joined room {room_id}")
            return {"room_id": int(room_id), "room_name": room_data['room_name']}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Join room error: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/api/rooms/my-rooms")
async def get_my_rooms(token: str):
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    if 'user_id' not in payload:
        raise HTTPException(status_code=401, detail="Invalid token format - please login again")
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT r.id, r.room_name, r.created_at, u.username as creator
                FROM rooms r
                JOIN room_members rm ON r.id = rm.room_id
                JOIN users u ON r.creator_id = u.id
                WHERE rm.user_id = %s
                ORDER BY r.created_at DESC
            """, (payload['user_id'],))
            rooms = cursor.fetchall()
            return {"rooms": rooms}
    except Exception as e:
        print(f"❌ Get rooms error: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/api/rooms/{room_id}/messages")
async def get_room_messages(room_id: int, token: str):
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    if 'user_id' not in payload:
        raise HTTPException(status_code=401, detail="Invalid token format - please login again")
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM room_members WHERE room_id = %s AND user_id = %s",
                (room_id, payload['user_id'])
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=403, detail="Not a member of this room")
            cursor.execute("""
                SELECT m.*, u.username
                FROM messages m
                JOIN users u ON m.sender_id = u.id
                WHERE m.room_id = %s
                ORDER BY m.timestamp ASC
                LIMIT 100
            """, (room_id,))
            messages = cursor.fetchall()
            return {"messages": messages}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Get messages error: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.post("/api/sign-to-text")
async def sign_to_text(file: UploadFile = File(...)):
    if not llm:
        raise HTTPException(status_code=500, detail="LLM not configured")
    try:
        image_bytes = await file.read()
        image_base64 = encode_image(image_bytes)
        response = llm.invoke([
            HumanMessage(
                content=[
                    {"type": "text", "text": "Analyze this American sign language gesture. Reply with ONLY the alphabet or word being signed. No explanation, just the letter/word in uppercase."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
                ]
            )
        ])
        return {"result": response.content.strip().upper()}
    except Exception as e:
        print(f"Sign to text error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/text-to-sign")
async def text_to_sign(text: str = Form(...)):
    """Returns sign language image URLs for given text"""
    sign_dict = {
        "A": "https://upload.wikimedia.org/wikipedia/commons/2/27/Sign_language_A.svg",
        "B": "https://upload.wikimedia.org/wikipedia/commons/1/18/Sign_language_B.svg",
        "C": "https://upload.wikimedia.org/wikipedia/commons/e/e3/Sign_language_C.svg",
        "D": "https://upload.wikimedia.org/wikipedia/commons/0/06/Sign_language_D.svg",
        "E": "https://upload.wikimedia.org/wikipedia/commons/c/cd/Sign_language_E.svg",
        "F": "https://upload.wikimedia.org/wikipedia/commons/8/8f/Sign_language_F.svg",
        "G": "https://upload.wikimedia.org/wikipedia/commons/d/d9/Sign_language_G.svg",
        "H": "https://upload.wikimedia.org/wikipedia/commons/9/97/Sign_language_H.svg",
        "I": "https://upload.wikimedia.org/wikipedia/commons/1/10/Sign_language_I.svg",
        "J": "https://upload.wikimedia.org/wikipedia/commons/b/b1/Sign_language_J.svg",
        "K": "https://upload.wikimedia.org/wikipedia/commons/9/97/Sign_language_K.svg",
        "L": "https://upload.wikimedia.org/wikipedia/commons/d/d2/Sign_language_L.svg",
        "M": "https://upload.wikimedia.org/wikipedia/commons/c/c4/Sign_language_M.svg",
        "N": "https://upload.wikimedia.org/wikipedia/commons/e/e6/Sign_language_N.svg",
        "O": "https://freesvg.org/img/Deaf-Alphabet-O.png",
        "P": "https://freesvg.org/img/Deaf-Alphabet-P.png",
        "Q": "https://freesvg.org/img/Deaf-Alphabet-Q.png",
        "R": "https://freesvg.org/img/Deaf-Alphabet-R.png",
        "S": "https://upload.wikimedia.org/wikipedia/commons/3/3f/Sign_language_S.svg",
        "T": "https://upload.wikimedia.org/wikipedia/commons/1/13/Sign_language_T.svg",
        "U": "https://upload.wikimedia.org/wikipedia/commons/7/7c/Sign_language_U.svg",
        "V": "https://freesvg.org/img/Deaf-Alphabet-V.png",
        "W": "https://upload.wikimedia.org/wikipedia/commons/8/83/Sign_language_W.svg",
        "X": "https://freesvg.org/img/Deaf-Alphabet-X.png",
        "Y": "https://upload.wikimedia.org/wikipedia/commons/1/1d/Sign_language_Y.svg",
        "Z": "https://freesvg.org/img/Deaf-Alphabet-Z.png"
    }
    signs = []
    for char in text.upper():
        if char in sign_dict:
            signs.append({"letter": char, "url": sign_dict[char]})
        elif char == " ":
            signs.append({"letter": " ", "url": None})
    return {"signs": signs}


# ─── NEW: Text to Emoji endpoint ────────────────────────────────────────────

EMOJI_PROMPTS = {
    "story": (
        "You are an emoji storyteller. Convert the given text into a creative emoji story "
        "that captures its full meaning, emotions, and narrative. Use 5–15 emojis. "
        "Response format (two lines only):\n"
        "EMOJIS: <only emojis here>\n"
        "EXPLAIN: <one sentence explaining what the emojis mean>"
    ),
    "single": (
        "You are an emoji expert. Find the SINGLE best emoji (or at most 3) that perfectly "
        "represents the given text. Choose the most expressive and fitting option. "
        "Response format (two lines only):\n"
        "EMOJIS: <only 1–3 emojis here>\n"
        "EXPLAIN: <one sentence explaining your choice>"
    ),
    "sentence": (
        "You are an emoji translator. Replace the significant words in the given text with "
        "fitting emojis. Keep small connector words as text. "
        "Response format (two lines only):\n"
        "EMOJIS: <mixed text and emojis>\n"
        "EXPLAIN: <brief note on what you translated>"
    ),
    "mood": (
        "You are an emoji mood artist. Create a mood board of 8–12 emojis that capture the "
        "feelings, atmosphere, themes, and essence of the given text. Space them out nicely. "
        "Response format (two lines only):\n"
        "EMOJIS: <only emojis here, space separated>\n"
        "EXPLAIN: <one sentence describing the overall mood>"
    ),
}

@app.post("/api/text-to-emoji")
async def text_to_emoji(request: TextToEmojiRequest):
    """Convert text to emoji using LLM."""
    if not llm:
        raise HTTPException(status_code=500, detail="LLM not configured")
    
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    
    mode = request.mode if request.mode in EMOJI_PROMPTS else "story"
    system_prompt = EMOJI_PROMPTS[mode]
    
    try:
        response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=f'Text: "{request.text}"')
        ])
        
        raw = response.content.strip()
        
        # Parse the two-line response
        emojis = ""
        explanation = ""
        
        for line in raw.split('\n'):
            line = line.strip()
            if line.upper().startswith("EMOJIS:"):
                emojis = line[7:].strip()
            elif line.upper().startswith("EXPLAIN:"):
                explanation = line[8:].strip()
        
        # Fallback if parsing fails
        if not emojis:
            emojis = raw.split('\n')[0].strip()
        
        return {
            "emojis": emojis,
            "explanation": explanation,
            "mode": mode
        }
        
    except Exception as e:
        print(f"Text to emoji error: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ─── WebSocket ──────────────────────────────────────────────────────────────

@app.websocket("/ws/{room_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, user_id: str):
    print(f"🔌 WebSocket connection attempt - Room: {room_id}, User: {user_id}")
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM room_members WHERE room_id = %s AND user_id = %s",
                (room_id, user_id)
            )
            if not cursor.fetchone():
                await websocket.close(code=4003, reason="Not a member of this room")
                return
    except Exception as e:
        print(f"Database error during WebSocket auth: {e}")
        await websocket.close(code=4000, reason="Database error")
        return
    finally:
        conn.close()
    
    await manager.connect(websocket, room_id, user_id)
    
    try:
        while True:
            data = await websocket.receive_json()
            print(f"📨 Received data: {data}")
            
            if data['type'] == 'chat_message':
                conn = get_db_connection()
                try:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "INSERT INTO messages (room_id, sender_id, message, message_type, timestamp) VALUES (%s, %s, %s, %s, %s)",
                            (room_id, user_id, data['message'], data.get('message_type', 'text'), datetime.now())
                        )
                        conn.commit()
                        message_id = cursor.lastrowid
                        
                        cursor.execute("SELECT username FROM users WHERE id = %s", (user_id,))
                        user = cursor.fetchone()
                        
                        message_data = {
                            'type': 'new_message',
                            'message_id': message_id,
                            'sender_id': int(user_id),
                            'username': user['username'],
                            'message': data['message'],
                            'message_type': data.get('message_type', 'text'),
                            'timestamp': datetime.now().isoformat()
                        }
                        
                        # Broadcast to ALL users in room including sender
                        await manager.send_to_room(room_id, message_data, sender_id=None)
                        
                except Exception as e:
                    print(f"Error saving message: {e}")
                    print(traceback.format_exc())
                finally:
                    conn.close()
                    
    except WebSocketDisconnect:
        manager.disconnect(room_id, user_id)
    except Exception as e:
        print(f"WebSocket error: {e}")
        print(traceback.format_exc())
        manager.disconnect(room_id, user_id)

if __name__ == "__main__":
    import uvicorn
    print("🚀 Starting SignBridge API Server...")
    print("📍 Server: http://localhost:8000")
    print("🔌 WebSocket: ws://localhost:8000/ws/{room_id}/{user_id}")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")