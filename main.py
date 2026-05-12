from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from database import supabase
import bcrypt
import uuid
import os
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
from typing import Optional

load_dotenv()

IMAGGA_KEY    = os.getenv("IMAGGA_KEY")
IMAGGA_SECRET = os.getenv("IMAGGA_SECRET")

app = FastAPI(title="Digital Asset Management API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Models ───────────────────────────────────────────
class RegisterInput(BaseModel):
    nama: str
    email: str
    password: str

class LoginInput(BaseModel):
    email: str
    password: str

class TagInput(BaseModel):
    nama_tag: str
    sumber: str

class FolderInput(BaseModel):
    nama: str
    parent_id: Optional[str] = None
    user_id: str

class RuleInput(BaseModel):
    user_id: str
    keyword: str
    folder_id: str

# ── Helper: auto tagging ─────────────────────────────
def tag_dari_nama_file(nama_file: str) -> list:
    stopwords = {"the","and","for","with","dari","dan","untuk","dengan","di","ke","yang","at","in","on"}
    nama   = os.path.splitext(nama_file)[0]
    tokens = nama.replace("-", "_").replace(" ", "_").split("_")
    return [t.lower() for t in tokens if t and t.lower() not in stopwords and len(t) > 1]

def tag_dari_tipe_file(tipe: str, ukuran: int) -> list:
    tags = []
    if "image" in tipe:
        tags.append("gambar")
        if "jpeg" in tipe or "jpg" in tipe: tags.append("jpg")
        elif "png" in tipe:  tags.append("png")
        elif "gif" in tipe:  tags.append("gif")
        elif "webp" in tipe: tags.append("webp")
    elif "video" in tipe:
        tags.append("video")
        if "mp4" in tipe:       tags.append("mp4")
        elif "quicktime" in tipe: tags.append("mov")
    elif "pdf" in tipe:
        tags.append("pdf")
        tags.append("dokumen")
    elif "word" in tipe or "document" in tipe:
        tags.append("word")
        tags.append("dokumen")
    elif "spreadsheet" in tipe or "excel" in tipe:
        tags.append("excel")
        tags.append("spreadsheet")

    mb = ukuran / (1024 * 1024)
    if mb < 1:    tags.append("file-kecil")
    elif mb < 10: tags.append("file-sedang")
    else:         tags.append("file-besar")

    return tags

def tag_dari_imagga(url_gambar: str) -> list:
    tags = []

    try:
        response = requests.get(
            "https://api.imagga.com/v2/tags",
            params={"image_url": url_gambar, "limit": 25, "threshold": 25, "language": "en"},
            auth=HTTPBasicAuth(IMAGGA_KEY, IMAGGA_SECRET),
            timeout=15
        )
        result = response.json()
        if result.get("status", {}).get("type") == "success":
            for item in result["result"]["tags"]:
                nama       = item["tag"]["en"].lower().replace(" ", "-")
                confidence = item["confidence"]
                if confidence >= 25 and nama not in tags:
                    tags.append(nama)
    except Exception as e:
        print(f"Imagga tags error: {e}")

    try:
        response = requests.get(
            "https://api.imagga.com/v2/colors",
            params={"image_url": url_gambar},
            auth=HTTPBasicAuth(IMAGGA_KEY, IMAGGA_SECRET),
            timeout=15
        )
        result = response.json()
        if result.get("status", {}).get("type") == "success":
            colors = result["result"]["colors"]
            for c in colors.get("image_colors", [])[:3]:
                nama_warna = c["closest_palette_color"].lower().replace(" ", "-")
                if f"warna-{nama_warna}" not in tags:
                    tags.append(f"warna-{nama_warna}")
            for c in colors.get("foreground_colors", [])[:2]:
                nama_warna = c["closest_palette_color"].lower().replace(" ", "-")
                tag_warna  = f"warna-{nama_warna}"
                if tag_warna not in tags:
                    tags.append(tag_warna)
    except Exception as e:
        print(f"Imagga colors error: {e}")

    try:
        response = requests.get(
            "https://api.imagga.com/v2/categories/personal_photos",
            params={"image_url": url_gambar},
            auth=HTTPBasicAuth(IMAGGA_KEY, IMAGGA_SECRET),
            timeout=15
        )
        result = response.json()
        if result.get("status", {}).get("type") == "success":
            for cat in result["result"]["categories"]:
                if cat["confidence"] >= 20:
                    nama_cat = cat["name"]["en"].lower().replace(" ", "-")
                    if f"kategori-{nama_cat}" not in tags:
                        tags.append(f"kategori-{nama_cat}")
    except Exception as e:
        print(f"Imagga categories error: {e}")

    return tags

def simpan_tags(asset_id: str, tags: list, sumber: str):
    for nama_tag in tags:
        nama_tag = nama_tag.strip()
        if not nama_tag:
            continue
        existing = supabase.table("tags").select("id").eq("nama", nama_tag).execute()
        if existing.data:
            tag_id = existing.data[0]["id"]
        else:
            result = supabase.table("tags").insert({"nama": nama_tag}).execute()
            tag_id = result.data[0]["id"]
        supabase.table("asset_tags").insert({
            "asset_id": asset_id,
            "tag_id":   tag_id,
            "sumber":   sumber
        }).execute()

def auto_assign_folder(asset_id: str, user_id: str, tags: list):
    rules = supabase.table("folder_rules").select("*, folders(nama)").eq("user_id", user_id).execute()
    if not rules.data:
        return
    for rule in rules.data:
        keyword = rule["keyword"].lower()
        for tag in tags:
            if keyword in tag.lower():
                already = supabase.table("asset_folders").select("id").eq("asset_id", asset_id).eq("folder_id", rule["folder_id"]).execute()
                if not already.data:
                    supabase.table("asset_folders").insert({
                        "asset_id": asset_id,
                        "folder_id": rule["folder_id"]
                    }).execute()
                break

# ── Endpoints ────────────────────────────────────────

@app.get("/")
def root():
    return {"message": "DAM API berjalan", "status": "ok"}

# Auth
@app.post("/auth/register")
def register(data: RegisterInput):
    existing = supabase.table("users").select("id").eq("email", data.email).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="Email sudah terdaftar")
    hashed = bcrypt.hashpw(data.password.encode(), bcrypt.gensalt()).decode()
    result = supabase.table("users").insert({
        "nama":     data.nama,
        "email":    data.email,
        "password": hashed
    }).execute()
    return {"message": "Registrasi berhasil", "user": result.data[0]}

@app.post("/auth/login")
def login(data: LoginInput):
    result = supabase.table("users").select("*").eq("email", data.email).execute()
    if not result.data:
        raise HTTPException(status_code=400, detail="Email atau password salah")
    user = result.data[0]
    if not bcrypt.checkpw(data.password.encode(), user["password"].encode()):
        raise HTTPException(status_code=400, detail="Email atau password salah")
    return {"message": "Login berhasil", "user_id": user["id"], "nama": user["nama"]}

# Assets
@app.post("/assets/upload")
async def upload_asset(
    user_id: str = Form(...),
    file: UploadFile = File(...)
):
    isi_file = await file.read()
    ukuran   = len(isi_file)
    ekstensi = os.path.splitext(file.filename)[1]
    nama_unik    = f"{uuid.uuid4()}{ekstensi}"
    path_storage = f"{user_id}/{nama_unik}"

    try:
        supabase.storage.from_("assets").upload(
            path=path_storage,
            file=isi_file,
            file_options={"content-type": file.content_type}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal upload ke storage: {str(e)}")

    url_publik = supabase.storage.from_("assets").get_public_url(path_storage)

    result = supabase.table("assets").insert({
        "user_id":   user_id,
        "nama_file": file.filename,
        "tipe_file": file.content_type,
        "ukuran":    ukuran,
        "url":       url_publik
    }).execute()

    asset_id = result.data[0]["id"]

    tags_nama = tag_dari_nama_file(file.filename)
    simpan_tags(asset_id, tags_nama, "nama_file")

    tags_tipe = tag_dari_tipe_file(file.content_type, ukuran)
    simpan_tags(asset_id, tags_tipe, "metadata")

    tags_ai = []
    if "image" in file.content_type:
        tags_ai = tag_dari_imagga(url_publik)
        simpan_tags(asset_id, tags_ai, "ai")

    semua_tags = tags_nama + tags_tipe + tags_ai
    auto_assign_folder(asset_id, user_id, semua_tags)

    return {
        "message": "Upload berhasil",
        "asset":   result.data[0],
        "tags":    list(set(semua_tags)),
        "tags_ai": tags_ai
    }

@app.get("/assets")
def get_assets(user_id: str):
    result = supabase.table("assets").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
    return {"assets": result.data}

@app.get("/assets/{asset_id}")
def get_asset_detail(asset_id: str):
    asset = supabase.table("assets").select("*").eq("id", asset_id).execute()
    if not asset.data:
        raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
    asset_tags = supabase.table("asset_tags").select("*, tags(nama)").eq("asset_id", asset_id).execute()
    tags = [{"nama": at["tags"]["nama"], "sumber": at["sumber"]} for at in asset_tags.data]
    return {"asset": asset.data[0], "tags": tags}

@app.delete("/assets/{asset_id}")
def delete_asset(asset_id: str):
    asset = supabase.table("assets").select("*").eq("id", asset_id).execute()
    if not asset.data:
        raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
    url  = asset.data[0]["url"]
    path = url.split("/object/public/assets/")[1]
    supabase.storage.from_("assets").remove([path])
    supabase.table("asset_tags").delete().eq("asset_id", asset_id).execute()
    supabase.table("asset_folders").delete().eq("asset_id", asset_id).execute()
    supabase.table("assets").delete().eq("id", asset_id).execute()
    return {"message": "Aset berhasil dihapus"}

@app.post("/assets/{asset_id}/tags")
def tambah_tag(asset_id: str, data: TagInput):
    existing_tag = supabase.table("tags").select("id").eq("nama", data.nama_tag).execute()
    if existing_tag.data:
        tag_id = existing_tag.data[0]["id"]
    else:
        result = supabase.table("tags").insert({"nama": data.nama_tag}).execute()
        tag_id = result.data[0]["id"]
    already = supabase.table("asset_tags").select("id").eq("asset_id", asset_id).eq("tag_id", tag_id).execute()
    if already.data:
        return {"message": "Tag sudah ada"}
    supabase.table("asset_tags").insert({
        "asset_id": asset_id,
        "tag_id":   tag_id,
        "sumber":   data.sumber
    }).execute()
    return {"message": "Tag ditambahkan"}

@app.delete("/assets/{asset_id}/tags/{nama_tag}")
def hapus_tag(asset_id: str, nama_tag: str):
    tag = supabase.table("tags").select("id").eq("nama", nama_tag).execute()
    if not tag.data:
        raise HTTPException(status_code=404, detail="Tag tidak ditemukan")
    tag_id = tag.data[0]["id"]
    supabase.table("asset_tags").delete().eq("asset_id", asset_id).eq("tag_id", tag_id).execute()
    return {"message": "Tag dihapus"}

# Tags
@app.get("/tags")
def get_tags_by_user(user_id: str):
    assets = supabase.table("assets").select("id").eq("user_id", user_id).execute()
    asset_ids = [a["id"] for a in assets.data]
    if not asset_ids:
        return {"tags": []}
    result = supabase.table("asset_tags").select("tag_id, sumber, tags(nama)").in_("asset_id", asset_ids).execute()
    seen = set()
    tags = []
    for item in result.data:
        nama = item["tags"]["nama"]
        if nama not in seen:
            seen.add(nama)
            tags.append({"nama": nama, "sumber": item["sumber"]})
    tags.sort(key=lambda x: x["nama"])
    return {"tags": tags}

@app.get("/tags/count")
def count_tags(user_id: str):
    assets = supabase.table("assets").select("id").eq("user_id", user_id).execute()
    asset_ids = [a["id"] for a in assets.data]
    if not asset_ids:
        return {"count": 0}
    result = supabase.table("asset_tags").select("tag_id").in_("asset_id", asset_ids).execute()
    unique_tags = set(at["tag_id"] for at in result.data)
    return {"count": len(unique_tags)}

@app.get("/tags/top")
def top_tags(user_id: str, limit: int = 7):
    assets = supabase.table("assets").select("id").eq("user_id", user_id).execute()
    asset_ids = [a["id"] for a in assets.data]
    if not asset_ids:
        return {"tags": []}
    result = supabase.table("asset_tags").select("tag_id, tags(nama)").in_("asset_id", asset_ids).execute()
    count = {}
    for item in result.data:
        nama = item["tags"]["nama"]
        count[nama] = count.get(nama, 0) + 1
    sorted_tags = sorted(count.items(), key=lambda x: x[1], reverse=True)[:limit]
    return {"tags": [{"nama": t[0], "jumlah": t[1]} for t in sorted_tags]}

# Search
@app.get("/search/by-tag")
def get_assets_by_tag(user_id: str, tag: str):
    tag_result = supabase.table("tags").select("id").eq("nama", tag).execute()
    if not tag_result.data:
        return {"asset_ids": []}
    tag_id = tag_result.data[0]["id"]
    assets = supabase.table("assets").select("id").eq("user_id", user_id).execute()
    user_asset_ids = [a["id"] for a in assets.data]
    at_result = supabase.table("asset_tags").select("asset_id").eq("tag_id", tag_id).in_("asset_id", user_asset_ids).execute()
    asset_ids = [at["asset_id"] for at in at_result.data]
    return {"asset_ids": asset_ids}

# Stats
@app.get("/stats/tagging")
def stats_tagging(user_id: str):
    assets = supabase.table("assets").select("id").eq("user_id", user_id).execute()
    asset_ids = [a["id"] for a in assets.data]
    if not asset_ids:
        return {"total": 0, "nama_file": 0, "metadata": 0, "ai": 0, "manual": 0}
    result = supabase.table("asset_tags").select("sumber").in_("asset_id", asset_ids).execute()
    total     = len(result.data)
    nama_file = sum(1 for r in result.data if r["sumber"] == "nama_file")
    metadata  = sum(1 for r in result.data if r["sumber"] == "metadata")
    ai        = sum(1 for r in result.data if r["sumber"] == "ai")
    manual    = sum(1 for r in result.data if r["sumber"] == "manual")
    return {
        "total":         total,
        "nama_file":     nama_file,
        "metadata":      metadata,
        "ai":            ai,
        "manual":        manual,
        "pct_nama_file": round(nama_file / total * 100) if total else 0,
        "pct_metadata":  round(metadata  / total * 100) if total else 0,
        "pct_ai":        round(ai        / total * 100) if total else 0,
        "pct_manual":    round(manual    / total * 100) if total else 0,
    }

# Folders
@app.post("/folders")
def buat_folder(data: FolderInput):
    payload = {"nama": data.nama, "user_id": data.user_id}
    if data.parent_id:
        payload["parent_id"] = data.parent_id
    result = supabase.table("folders").insert(payload).execute()
    return {"message": "Folder dibuat", "folder": result.data[0]}

@app.get("/folders")
def get_folders(user_id: str):
    result = supabase.table("folders").select("*").eq("user_id", user_id).order("nama").execute()
    return {"folders": result.data}

@app.delete("/folders/{folder_id}")
def hapus_folder(folder_id: str):
    supabase.table("asset_folders").delete().eq("folder_id", folder_id).execute()
    supabase.table("folder_rules").delete().eq("folder_id", folder_id).execute()
    supabase.table("folders").delete().eq("id", folder_id).execute()
    return {"message": "Folder dihapus"}

@app.post("/folders/rules")
def buat_rule(data: RuleInput):
    result = supabase.table("folder_rules").insert({
        "user_id":   data.user_id,
        "keyword":   data.keyword.lower(),
        "folder_id": data.folder_id
    }).execute()
    return {"message": "Rule ditambahkan", "rule": result.data[0]}

@app.get("/folders/rules")
def get_rules(user_id: str):
    result = supabase.table("folder_rules").select("*, folders(nama)").eq("user_id", user_id).execute()
    return {"rules": result.data}

@app.delete("/folders/rules/{rule_id}")
def hapus_rule(rule_id: str):
    supabase.table("folder_rules").delete().eq("id", rule_id).execute()
    return {"message": "Rule dihapus"}

@app.get("/folders/{folder_id}/assets")
def get_assets_by_folder(folder_id: str):
    result = supabase.table("asset_folders").select("asset_id, assets(*)").eq("folder_id", folder_id).execute()
    assets = [r["assets"] for r in result.data if r["assets"]]
    return {"assets": assets}