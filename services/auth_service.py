"""Password and cookie-session helpers shared by auth routes."""
import hashlib, hmac, secrets
from fastapi import HTTPException, Request, Response
from database.repository import q
def hp(pw, salt): return hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2**14, r=8, p=1).hex()

def me(req: Request):
    r = q("select uid from sessions where tok=?", (req.cookies.get("t"),))
    if not r: raise HTTPException(401, "Please log in")
    return r[0][0]

def session(res, uid):
    t = secrets.token_hex(16); q("insert into sessions values(?,?)", (t, uid))
    res.set_cookie("t", t, httponly=True, samesite="lax")

def passwords_match(candidate, stored, salt):
    return hmac.compare_digest(hp(candidate, salt), stored)

