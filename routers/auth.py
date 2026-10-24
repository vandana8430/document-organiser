"""Registration, login, and logout endpoints."""
import re, secrets
import sqlite3
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from database.repository import q
from services.auth_service import hp, me, passwords_match, session
router = APIRouter()
@router.post("/api/register")
def register(b: dict, res: Response):
    if not isinstance(b, dict): raise HTTPException(422, "Registration details are required")
    email = b.get("email")
    password = b.get("password")
    if not isinstance(email, str):
        raise HTTPException(422, "Enter a valid email address")
    email = email.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(422, "Enter a valid email address")
    if not isinstance(password, str) or len(password) < 8 or len(password) > 1024:
        raise HTTPException(422, "Password must contain between 8 and 1,024 characters")
    salt = secrets.token_hex(16)
    try: q("insert into users(email,pw,salt) values(?,?,?)", (email, hp(password, salt), salt))
    except sqlite3.IntegrityError: raise HTTPException(400, "Email already registered")
    session(res, q("select id from users where email=?", (email,))[0][0]); return {"ok": 1}

@router.post("/api/login")
def login(b: dict, res: Response):
    if not isinstance(b, dict): raise HTTPException(422, "Login details are required")
    email, password = b.get("email"), b.get("password")
    if not isinstance(email, str) or not isinstance(password, str) or len(password) > 1024:
        raise HTTPException(422, "Email and password are required")
    r = q("select * from users where lower(email)=lower(?)", (email.strip(),))
    if not r or not passwords_match(password, r[0]["pw"], r[0]["salt"]): raise HTTPException(401, "Wrong email or password")
    session(res, r[0]["id"]); return {"ok": 1}

@router.post("/api/logout")
def logout(req: Request, res: Response):
    q("delete from sessions where tok=?", (req.cookies.get("t"),)); res.delete_cookie("t"); return {"ok": 1}

@router.get("/api/me")
def current_user(uid: int = Depends(me)):
    rows = q("select id,email from users where id=?", (uid,))
    if not rows: raise HTTPException(401, "Please log in")
    return {"id": rows[0]["id"], "email": rows[0]["email"]}

