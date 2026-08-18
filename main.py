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
import re
import mimetypes

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
    user_id: Optional[str] = None

class DownloadLogInput(BaseModel):
    asset_id: str
    user_id: str

class FolderInput(BaseModel):
    nama: str
    parent_id: Optional[str] = None
    user_id: str
    target_division_id: Optional[str] = None
    division_ids: Optional[list] = None

class FolderDivisionInput(BaseModel):
    division_ids: list
    granted_by: str

class FolderAccessInput(BaseModel):
    user_ids: list
    granted_by: str

class MoveAssetInput(BaseModel):
    user_id: str
    to_folder_id: str
    from_folder_id: Optional[str] = None

class RuleInput(BaseModel):
    user_id: str
    keyword: str
    folder_id: str

class RuleBatchInput(BaseModel):
    user_id: str
    keywords: list
    folder_id: str

class AssignMatchingAssetsInput(BaseModel):
    user_id: str
    asset_ids: list

class MoveFolderInput(BaseModel):
    user_id: str
    parent_id: Optional[str] = None

class PredictFoldersInput(BaseModel):
    division_ids: list

class ShareInput(BaseModel):
    asset_id: str
    from_user_id: str
    to_division_id: str
    catatan: Optional[str] = None

class FolderShareInput(BaseModel):
    from_user_id: str
    to_division_id: str

class PermissionInput(BaseModel):
    asset_id: str
    division_ids: list

class UpdateProfileInput(BaseModel):
    nama: str

class UpdatePasswordInput(BaseModel):
    password_lama: str
    password_baru: str

class AdminUpdateUserInput(BaseModel):
    nama: Optional[str] = None
    password_baru: Optional[str] = None

# ── Division Config ───────────────────────────────────
MANAGER_ID = "79732e94-d800-4b11-ad92-74e594f1b54b"

# ── Helpers ───────────────────────────────────────────
def get_user_division(user_id: str):
    result = supabase.table("user_divisions").select("*, divisions(*)").eq("user_id", user_id).execute()
    if not result.data:
        return None
    return result.data[0]

def is_manager(user_id: str) -> bool:
    ud = get_user_division(user_id)
    if not ud:
        return False
    return ud["division_id"] == MANAGER_ID

def has_folder_access(user_id: str, folder_id: str) -> bool:
    result = supabase.table("folder_access").select("id").eq("folder_id", folder_id).eq("user_id", user_id).execute()
    return len(result.data) > 0

def get_folder_division_ids(folder_id: str) -> list:
    result = supabase.table("folder_divisions").select("division_id").eq("folder_id", folder_id).execute()
    return [r["division_id"] for r in result.data]

def can_access_folder(user_id: str, folder: dict) -> bool:
    if is_manager(user_id):
        return True
    if folder.get("user_id") == user_id:
        return True
    ud = get_user_division(user_id)
    division_id = ud["division_id"] if ud else None
    if folder.get("type") == "shared":
        return True
    if folder.get("division_id") and division_id == folder.get("division_id"):
        return True
    if division_id and division_id in get_folder_division_ids(folder["id"]):
        return True
    if has_folder_access(user_id, folder["id"]):
        return True
    return False

def can_access_asset(user_id: str, asset: dict) -> bool:
    """Cek apakah user boleh mengakses sebuah aset.

    Aturan akses:
    - Manager boleh mengakses semua aset.
    - Uploader selalu boleh mengakses aset miliknya.
    - Staff boleh mengakses aset yang division_id-nya sama dengan divisinya.
    - asset_permissions dapat memberi akses tambahan lintas divisi.
    - asset_shares dapat memberi akses tambahan melalui fitur Shared with me.

    Catatan:
    is_public TIDAK lagi dipakai sebagai akses global antar-divisi.
    Field tersebut tetap dibiarkan di database untuk kompatibilitas data lama.
    """
    if is_manager(user_id):
        return True

    if asset.get("user_id") == user_id:
        return True

    ud = get_user_division(user_id)
    division_id = ud["division_id"] if ud else None
    if not division_id:
        return False

    # Aset milik divisi user sendiri.
    if asset.get("division_id") == division_id:
        return True

    # Permission eksplisit, misalnya Manager menaruh aset ke beberapa divisi.
    permission = supabase.table("asset_permissions").select("id").eq(
        "asset_id", asset["id"]
    ).eq(
        "division_id", division_id
    ).execute()
    if permission.data:
        return True

    # Share eksplisit ke divisi user.
    share = supabase.table("asset_shares").select("id").eq(
        "asset_id", asset["id"]
    ).eq(
        "to_division_id", division_id
    ).execute()

    return bool(share.data)

# ── Auto Tagging ──────────────────────────────────────
def tag_dari_nama_file(nama_file: str) -> list:
    stopwords = {"the","and","for","with","dari","dan","untuk","dengan","di","ke","yang","at","in","on"}
    nama   = os.path.splitext(nama_file)[0]
    tokens = nama.replace("-", "_").replace(" ", "_").split("_")
    return [t.lower() for t in tokens if t and t.lower() not in stopwords and len(t) > 1]

def tag_dari_tipe_file(tipe: str, ukuran: int, nama_file: str = "") -> list:
    tags = []
    tipe = (tipe or "").lower()
    ekstensi = os.path.splitext(nama_file or "")[1].lower()

    if "image" in tipe:
        tags.append("gambar")
        if "jpeg" in tipe or "jpg" in tipe: tags.append("jpg")
        elif "png" in tipe:  tags.append("png")
        elif "gif" in tipe:  tags.append("gif")
        elif "webp" in tipe: tags.append("webp")
    elif "video" in tipe:
        tags.append("video")
        if "mp4" in tipe or ekstensi == ".mp4":         tags.append("mp4")
        elif "quicktime" in tipe or ekstensi == ".mov": tags.append("mov")
    elif "audio" in tipe or ekstensi in {".mp3", ".wav"}:
        tags.append("audio")
        if ekstensi == ".mp3" or "mpeg" in tipe or "mp3" in tipe:
            tags.append("mp3")
        elif ekstensi == ".wav" or "wav" in tipe:
            tags.append("wav")
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

def log_activity(type_: str, user_id: str, asset_id: str = None, detail: dict = None):
    """Catat satu baris activity_log. Dibungkus try/except supaya kalau gagal
    (mis. koneksi putus), aksi utamanya (hapus/pindah/dsb) tetap jalan."""
    if not user_id:
        return
    try:
        supabase.table("activity_log").insert({
            "type":     type_,
            "user_id":  user_id,
            "asset_id": asset_id,
            "detail":   detail or {}
        }).execute()
    except Exception as e:
        print(f"Gagal mencatat activity log ({type_}): {e}")

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

def get_asset_tag_names(asset_id: str) -> list:
    """Ambil semua nama tag yang sudah tersimpan untuk satu aset."""
    result = supabase.table("asset_tags").select("tags(nama)").eq("asset_id", asset_id).execute()
    return [r["tags"]["nama"] for r in result.data if r.get("tags")]

def get_division_system_folders(division_ids: list) -> list:
    """Folder system bawaan tiap divisi di daftar division_ids."""
    if not division_ids:
        return []
    result = supabase.table("folders").select("id, nama, division_id").in_("division_id", division_ids).eq("type", "system").execute()
    if not result.data:
        return []
    div_rows = supabase.table("divisions").select("id, nama").in_("id", division_ids).execute()
    div_map  = {d["id"]: d["nama"] for d in div_rows.data}
    return [{
        "id":     f["id"],
        "nama":   f["nama"],
        "type":   "system",
        "reason": f'Folder divisi {div_map.get(f["division_id"], "kamu")}'
    } for f in result.data]

def _matching_smart_folders_from_rules(tags: list, division_ids: list, rules: list) -> list:
    """Hitung Smart Folder yang cocok sekaligus kekuatan kecocokannya.

    Satu folder bisa memiliki banyak rule. Semua rule yang cocok dikumpulkan agar
    rekomendasi dapat diranking berdasarkan jumlah rule/tag yang cocok, bukan
    berdasarkan urutan row folder_rules di database.
    """
    normalized_tags = list(dict.fromkeys([
        str(tag or "").strip().lower()
        for tag in (tags or [])
        if str(tag or "").strip()
    ]))

    folder_stats = {}

    for rule in rules or []:
        folder = rule.get("folders")
        if not folder:
            continue

        in_scope = folder.get("type") == "shared" or folder.get("division_id") in division_ids
        if not in_scope:
            continue

        keyword = str(rule.get("keyword") or "").strip().lower()
        if not keyword:
            continue

        fid = folder["id"]
        stat = folder_stats.setdefault(fid, {
            "folder": folder,
            "rule_keywords": set(),
            "matched_keywords": set(),
            "matched_tags": set(),
        })

        stat["rule_keywords"].add(keyword)

        matched_tag = next((tag for tag in normalized_tags if keyword in tag), None)
        if matched_tag:
            stat["matched_keywords"].add(keyword)
            stat["matched_tags"].add(matched_tag)

    matches = []

    for stat in folder_stats.values():
        if not stat["matched_keywords"]:
            continue

        folder = stat["folder"]
        matched_tags = [tag for tag in normalized_tags if tag in stat["matched_tags"]]
        matched_keywords = sorted(stat["matched_keywords"])
        rule_count = len(stat["rule_keywords"])
        match_count = len(stat["matched_keywords"])
        match_ratio = (match_count / rule_count) if rule_count else 0

        if match_ratio >= 1:
            confidence = "Sangat cocok"
        elif match_ratio >= 0.5:
            confidence = "Cocok"
        else:
            confidence = "Kecocokan rendah"

        matches.append({
            "id": folder["id"],
            "nama": folder["nama"],
            "type": folder.get("type"),
            "division_id": folder.get("division_id"),
            "parent_id": folder.get("parent_id"),
            "matched_tags": matched_tags,
            "matched_keywords": matched_keywords,
            "match_count": match_count,
            "rule_count": rule_count,
            "match_ratio": round(match_ratio, 4),
            "confidence": confidence,
            "reason": f'{match_count}/{rule_count} tag cocok · {", ".join(matched_tags)}'
        })

    return matches


def find_matching_smart_folders(tags: list, division_ids: list) -> list:
    """Smart folder (folder_rules) yang keyword-nya cocok salah satu tag,
    dibatasi hanya ke folder shared atau folder milik salah satu division_ids.

    parent_id ikut diambil agar hasil prediksi bisa dipangkas ke folder terdalam.
    """
    rules = supabase.table("folder_rules").select(
        "*, folders(id, nama, type, division_id, parent_id)"
    ).execute()
    return _matching_smart_folders_from_rules(tags, division_ids, rules.data or [])


def _folder_ancestor_ids(folder_id: str) -> set:
    """Ambil seluruh ancestor sebuah folder berdasarkan parent_id."""
    ancestors = set()
    current = _get_folder(folder_id)
    seen = set()

    while current and current.get("parent_id"):
        parent_id = current.get("parent_id")

        if parent_id in seen:
            break

        seen.add(parent_id)
        ancestors.add(parent_id)
        current = _get_folder(parent_id)

    return ancestors


def _keep_deepest_folder_suggestions(system_folders: list, smart_folders: list) -> list:
    """Hindari satu aset masuk ke parent dan child sekaligus.

    Aturan:
    - Jika ada Smart Folder yang cocok di suatu divisi, system folder divisi itu
      tidak ikut dimasukkan ke asset_folders. division_id pada asset sudah cukup
      untuk menunjukkan bahwa asset tersebut milik divisi itu.
    - Jika parent Smart Folder dan child Smart Folder sama-sama cocok, hanya child
      yang dipertahankan.
    - Smart Folder sibling yang sama-sama cocok tetap boleh dipertahankan.
    """
    if not smart_folders:
        return system_folders

    matched_smart_ids = {f["id"] for f in smart_folders}
    ancestor_ids_of_matches = set()

    for folder in smart_folders:
        ancestor_ids_of_matches.update(_folder_ancestor_ids(folder["id"]))

    deepest_smart = [
        folder
        for folder in smart_folders
        if folder["id"] not in ancestor_ids_of_matches
    ]

    smart_division_ids = {
        folder.get("division_id")
        for folder in deepest_smart
        if folder.get("division_id")
    }

    kept_system = [
        folder
        for folder in system_folders
        if folder.get("division_id") not in smart_division_ids
    ]

    return kept_system + deepest_smart


def _folder_depth(folder_id: str) -> int:
    """Kedalaman folder untuk tie-break ranking rekomendasi."""
    return len(_folder_ancestor_ids(folder_id))


def _rank_folder_suggestions(suggestions: list, auto_select_limit: int = 3) -> list:
    """Urutkan rekomendasi dari yang paling relevan dan tandai top-N default.

    Prioritas ranking:
    1. jumlah rule/tag yang cocok;
    2. persentase rule folder yang cocok;
    3. folder yang lebih dalam/spesifik;
    4. nama folder A-Z sebagai tie-breaker stabil.
    """
    enriched = []

    for suggestion in suggestions or []:
        item = dict(suggestion)
        if item.get("type") == "smart":
            item["depth"] = _folder_depth(item["id"])
        else:
            item.setdefault("match_count", 0)
            item.setdefault("rule_count", 0)
            item.setdefault("match_ratio", 0)
            item.setdefault("matched_tags", [])
            item.setdefault("matched_keywords", [])
            item.setdefault("confidence", "Folder divisi")
            item["depth"] = 0
        enriched.append(item)

    enriched.sort(key=lambda item: (
        -int(item.get("match_count") or 0),
        -float(item.get("match_ratio") or 0),
        -int(item.get("depth") or 0),
        str(item.get("nama") or "").lower(),
    ))

    for index, item in enumerate(enriched, start=1):
        item["rank"] = index
        item["auto_selected"] = index <= auto_select_limit

    return enriched


def predict_folders(tags: list, division_ids: list) -> list:
    """Hitung dan ranking folder tujuan auto-assign.

    Hasil hierarchy-aware: bila parent dan child sama-sama cocok, parent dibuang.
    Sibling yang cocok tetap dipertahankan, lalu diurutkan berdasarkan kekuatan
    kecocokan. Maksimal 3 rekomendasi teratas ditandai sebagai default.
    """
    system_folders = get_division_system_folders(division_ids)
    smart_folders  = find_matching_smart_folders(tags, division_ids)

    # Folder system lama dari helper belum membawa division_id ke output.
    # Lengkapi supaya pruning berdasarkan divisi bisa dilakukan.
    if system_folders:
        system_rows = supabase.table("folders").select(
            "id, division_id"
        ).in_("id", [f["id"] for f in system_folders]).execute()
        division_map = {row["id"]: row.get("division_id") for row in (system_rows.data or [])}
        for folder in system_folders:
            folder["division_id"] = division_map.get(folder["id"])

    hierarchy_filtered = _keep_deepest_folder_suggestions(system_folders, smart_folders)
    return _rank_folder_suggestions(hierarchy_filtered, auto_select_limit=3)

def _system_folder_ids_for_division(division_id: str) -> set:
    if not division_id:
        return set()
    result = supabase.table("folders").select("id").eq(
        "division_id", division_id
    ).eq("type", "system").execute()
    return {row["id"] for row in (result.data or []) if row.get("id")}


def _prune_ancestor_assignments(asset_id: str, target_folder_ids: list):
    """Saat aset masuk ke Smart Folder yang lebih spesifik, hapus assignment parent/root.

    Sibling folder tidak disentuh. Ini mencegah aset tampil ganda di Folder Divisi
    sekaligus di Smart Folder child setelah backfill atau penambahan tag manual.
    """
    target_ids = {folder_id for folder_id in (target_folder_ids or []) if folder_id}
    removable = set()

    for folder_id in target_ids:
        folder = _get_folder(folder_id)
        if not folder or folder.get("type") != "smart":
            continue

        removable.update(_folder_ancestor_ids(folder_id))

        # Smart Folder legacy bisa belum punya parent_id meski memiliki division_id.
        if not folder.get("parent_id") and folder.get("division_id"):
            removable.update(_system_folder_ids_for_division(folder.get("division_id")))

    removable.difference_update(target_ids)
    if removable:
        supabase.table("asset_folders").delete().eq(
            "asset_id", asset_id
        ).in_("folder_id", list(removable)).execute()


def commit_folder_assignments(asset_id: str, folder_ids: list, prune_ancestors: bool = False) -> list:
    """Terapkan daftar folder_id ke asset_folders secara idempoten.

    Return berisi folder_id yang benar-benar baru ditambahkan.
    """
    unique_ids = list(dict.fromkeys([folder_id for folder_id in (folder_ids or []) if folder_id]))

    if prune_ancestors and unique_ids:
        _prune_ancestor_assignments(asset_id, unique_ids)

    added = []
    for folder_id in unique_ids:
        already = supabase.table("asset_folders").select("id").eq(
            "asset_id", asset_id
        ).eq("folder_id", folder_id).execute()
        if already.data:
            continue

        supabase.table("asset_folders").insert({
            "asset_id": asset_id,
            "folder_id": folder_id
        }).execute()
        added.append(folder_id)

    return added


def auto_assign_folder(asset_id: str, tags: list, division_id: str = None) -> list:
    """Prediksi folder tujuan dan terapkan maksimal 3 rekomendasi terbaik."""
    division_ids = [division_id] if division_id else []
    suggestions = predict_folders(tags, division_ids)
    selected = [suggestion for suggestion in suggestions if suggestion.get("auto_selected")]
    commit_folder_assignments(
        asset_id,
        [suggestion["id"] for suggestion in selected],
        prune_ancestors=True
    )
    return suggestions


def auto_assign_asset_from_current_tags(asset_id: str) -> list:
    """Evaluasi ulang sebuah aset setelah tag manual ditambahkan.

    Hanya Smart Folder yang benar-benar baru ditambahkan yang dikembalikan supaya
    frontend dapat memberi feedback yang relevan kepada user.
    """
    asset_result = supabase.table("assets").select("id, division_id").eq("id", asset_id).execute()
    if not asset_result.data:
        return []

    asset = asset_result.data[0]
    division_id = asset.get("division_id")
    if not division_id:
        return []

    tags = get_asset_tag_names(asset_id)
    suggestions = predict_folders(tags, [division_id])
    selected = [suggestion for suggestion in suggestions if suggestion.get("auto_selected")]
    added_ids = set(commit_folder_assignments(
        asset_id,
        [suggestion["id"] for suggestion in selected],
        prune_ancestors=True
    ))

    return [
        suggestion
        for suggestion in suggestions
        if suggestion.get("type") == "smart" and suggestion.get("id") in added_ids
    ]

# ── Root ─────────────────────────────────────────────
@app.get("/")
def root():
    return {"message": "DAM API berjalan", "status": "ok"}

# ── Auth ─────────────────────────────────────────────
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

@app.post("/auth/register-with-division")
def register_with_division(data: RegisterInput, division_id: str):
    existing = supabase.table("users").select("id").eq("email", data.email).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="Email sudah terdaftar")
    hashed = bcrypt.hashpw(data.password.encode(), bcrypt.gensalt()).decode()
    result = supabase.table("users").insert({
        "nama":     data.nama,
        "email":    data.email,
        "password": hashed
    }).execute()
    user_id = result.data[0]["id"]
    supabase.table("user_divisions").insert({
        "user_id":     user_id,
        "division_id": division_id
    }).execute()
    return {"message": "Registrasi berhasil", "user": result.data[0]}

# ── Assets ───────────────────────────────────────────
@app.post("/assets/upload")
async def upload_asset(
    user_id: str = Form(...),
    file: UploadFile = File(...),
    target_division_id: Optional[str] = Form(None)
):
    isi_file = await file.read()
    ukuran   = len(isi_file)
    ekstensi = os.path.splitext(file.filename or "")[1].lower()


    # MIME type adalah identitas teknis file. MP3 secara standar menggunakan
    # audio/mpeg; ekstensi asli tetap disimpan di nama_file dan path storage.
    tipe_file = (file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream").lower()
    if ekstensi == ".mp3":
        tipe_file = "audio/mpeg"
    elif ekstensi == ".wav":
        tipe_file = "audio/wav"

    nama_unik    = f"{uuid.uuid4()}{ekstensi}"
    path_storage = f"{user_id}/{nama_unik}"

    try:
        supabase.storage.from_("assets").upload(
            path=path_storage,
            file=isi_file,
            file_options={"content-type": tipe_file}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal upload ke storage: {str(e)}")

    url_publik  = supabase.storage.from_("assets").get_public_url(path_storage)
    ud          = get_user_division(user_id)
    division_id = ud["division_id"] if ud else None

    # Manager/admin bisa upload langsung ke divisi lain.
    # Semua aset sekarang bersifat terisolasi per divisi.
    # is_public tidak lagi berarti "terlihat oleh semua divisi".
    is_cross_division = False
    if target_division_id and target_division_id != division_id:
        if not is_manager(user_id):
            raise HTTPException(status_code=403, detail="Hanya Manager yang bisa upload ke divisi lain")
        division_id = target_division_id
        is_cross_division = True

    result = supabase.table("assets").insert({
        "user_id":     user_id,
        "division_id": division_id,
        "nama_file":   file.filename,
        "tipe_file":   tipe_file,
        "ukuran":      ukuran,
        "url":         url_publik,
        # Aset tidak lagi otomatis menjadi global/public antar-divisi.
        "is_public":   False
    }).execute()

    asset_id = result.data[0]["id"]

    # Permission eksplisit tetap dibuat untuk upload Manager ke divisi lain.
    # Untuk upload biasa, division_id pada row assets sudah menjadi sumber akses utama.
    if is_cross_division:
        supabase.table("asset_permissions").insert({
            "asset_id":    asset_id,
            "division_id": division_id
        }).execute()

    tags_nama = tag_dari_nama_file(file.filename)
    simpan_tags(asset_id, tags_nama, "nama_file")

    tags_tipe = tag_dari_tipe_file(tipe_file, ukuran, file.filename)
    simpan_tags(asset_id, tags_tipe, "metadata")

    tags_ai = []
    if "image" in tipe_file:
        tags_ai = tag_dari_imagga(url_publik)
        simpan_tags(asset_id, tags_ai, "ai")

    semua_tags = tags_nama + tags_tipe + tags_ai
    suggested_folders = auto_assign_folder(asset_id, semua_tags, division_id)

    return {
        "message": "Upload berhasil",
        "asset":   result.data[0],
        "tags":    list(set(semua_tags)),
        "tags_ai": tags_ai,
        "suggested_folders": suggested_folders
    }

@app.post("/assets/{asset_id}/predict-folders")
def predict_folders_endpoint(asset_id: str, data: PredictFoldersInput):
    """Dipakai saat Manager upload ke beberapa divisi sekaligus: hitung folder
    yang relevan (folder system + smart folder yang cocok tag) untuk divisi-divisi
    tambahan itu, supaya bisa ditampilkan & dipilih di UI seperti divisi utama."""
    tags = get_asset_tag_names(asset_id)
    return {"suggested_folders": predict_folders(tags, data.division_ids)}

@app.get("/assets")
def get_assets(user_id: str):
    result = supabase.table("assets").select("*, users(nama)").eq("user_id", user_id).order("created_at", desc=True).execute()
    assets = []
    for a in result.data:
        item = {**a, "uploader": a.get("users", {}).get("nama", "—") if a.get("users") else "—"}
        item.pop("users", None)
        assets.append(item)
    return {"assets": assets}

@app.get("/assets/by-division")
def get_assets_by_division(user_id: str):
    """Aset yang tampil di root Gallery.

    Manager:
      - melihat semua aset.

    Staff:
      - melihat aset milik divisinya sendiri;
      - melihat aset yang diberi asset_permissions ke divisinya;
      - TIDAK otomatis melihat aset divisi lain hanya karena is_public=True;
      - aset yang dibagikan lewat asset_shares tetap muncul di "Shared with me",
        bukan dicampur ke root Gallery.
    """
    ud = get_user_division(user_id)
    if not ud:
        return {"assets": []}

    division_id = ud["division_id"]

    # Manager tetap mendapat global view.
    if division_id == MANAGER_ID:
        result = (
            supabase.table("assets")
            .select("*, users(nama)")
            .order("created_at", desc=True)
            .execute()
        )

        assets = []
        for a in result.data or []:
            item = {
                **a,
                "uploader": (
                    a.get("users", {}).get("nama", "—")
                    if a.get("users")
                    else "—"
                )
            }
            item.pop("users", None)
            assets.append(item)

        return {"assets": assets}

    # 1) Aset yang memang dimiliki divisi user.
    own_division_result = (
        supabase.table("assets")
        .select("*, users(nama)")
        .eq("division_id", division_id)
        .order("created_at", desc=True)
        .execute()
    )

    # 2) Aset tambahan yang secara eksplisit diberi permission ke divisi user.
    perm_result = (
        supabase.table("asset_permissions")
        .select("asset_id")
        .eq("division_id", division_id)
        .execute()
    )

    perm_asset_ids = list({
        row["asset_id"]
        for row in (perm_result.data or [])
        if row.get("asset_id")
    })

    permission_assets = []
    if perm_asset_ids:
        permission_result = (
            supabase.table("assets")
            .select("*, users(nama)")
            .in_("id", perm_asset_ids)
            .order("created_at", desc=True)
            .execute()
        )
        permission_assets = permission_result.data or []

    # Jangan campurkan asset_shares ke sini.
    # Gallery frontend sudah punya endpoint /assets/shared-to-me khusus.
    combined = {}

    for a in (own_division_result.data or []) + permission_assets:
        item = {
            **a,
            "uploader": (
                a.get("users", {}).get("nama", "—")
                if a.get("users")
                else "—"
            )
        }
        item.pop("users", None)
        combined[item["id"]] = item

    assets = list(combined.values())
    assets.sort(
        key=lambda item: item.get("created_at") or "",
        reverse=True
    )

    return {"assets": assets}

@app.get("/assets/shared-to-me")
def get_shared_to_me(user_id: str):
    ud = get_user_division(user_id)
    if not ud:
        return {"shares": []}
    division_id = ud["division_id"]
    result = supabase.table("asset_shares").select(
        "*, assets(*), users!asset_shares_from_user_id_fkey(nama), divisions(nama)"
    ).eq("to_division_id", division_id).order("created_at", desc=True).execute()
    return {"shares": result.data}

@app.get("/assets/shared-by-me")
def get_shared_by_me(user_id: str):
    result = supabase.table("asset_shares").select(
        "*, assets(*), divisions(nama)"
    ).eq("from_user_id", user_id).order("created_at", desc=True).execute()
    return {"shares": result.data}

@app.get("/assets/unread-shares")
def get_unread_shares(user_id: str):
    ud = get_user_division(user_id)
    if not ud:
        return {"count": 0}
    division_id = ud["division_id"]
    result = supabase.table("asset_shares").select("id").eq("to_division_id", division_id).eq("is_read", False).execute()
    return {"count": len(result.data)}

@app.post("/assets/share")
def share_asset(data: ShareInput):
    asset_result = supabase.table("assets").select("*").eq("id", data.asset_id).execute()
    if not asset_result.data:
        raise HTTPException(status_code=404, detail="Aset tidak ditemukan")

    asset = asset_result.data[0]
    if not can_access_asset(data.from_user_id, asset):
        raise HTTPException(status_code=403, detail="Tidak punya izin membagikan aset ini")

    target_division = supabase.table("divisions").select("id, nama").eq("id", data.to_division_id).execute()
    if not target_division.data:
        raise HTTPException(status_code=404, detail="Divisi tujuan tidak ditemukan")

    existing = supabase.table("asset_shares").select("id").eq(
        "asset_id", data.asset_id
    ).eq(
        "from_user_id", data.from_user_id
    ).eq(
        "to_division_id", data.to_division_id
    ).execute()

    payload = {
        "catatan": data.catatan,
        "is_read": False
    }

    if existing.data:
        result = supabase.table("asset_shares").update(payload).eq("id", existing.data[0]["id"]).execute()
    else:
        payload.update({
            "asset_id": data.asset_id,
            "from_user_id": data.from_user_id,
            "to_division_id": data.to_division_id
        })
        result = supabase.table("asset_shares").insert(payload).execute()

    return {
        "message": "Aset berhasil dibagikan",
        "share": result.data[0],
        "to_division": target_division.data[0]
    }

@app.put("/assets/share/{share_id}/read")
def mark_share_read(share_id: str):
    supabase.table("asset_shares").update({"is_read": True}).eq("id", share_id).execute()
    return {"message": "Ditandai sudah dibaca"}

@app.post("/assets/permissions")
def set_permissions(data: PermissionInput):
    supabase.table("asset_permissions").delete().eq("asset_id", data.asset_id).execute()
    if not data.division_ids:
        supabase.table("assets").update({"is_public": True}).eq("id", data.asset_id).execute()
        return {"message": "Aset dijadikan publik"}
    supabase.table("assets").update({"is_public": False}).eq("id", data.asset_id).execute()
    for div_id in data.division_ids:
        supabase.table("asset_permissions").insert({
            "asset_id":    data.asset_id,
            "division_id": div_id
        }).execute()
    return {"message": "Permission diset"}

@app.get("/assets/permissions/{asset_id}")
def get_permissions(asset_id: str):
    result = supabase.table("asset_permissions").select("*, divisions(nama)").eq("asset_id", asset_id).execute()
    return {"permissions": result.data}

@app.get("/assets/{asset_id}")
def get_asset_detail(asset_id: str):
    asset = supabase.table("assets").select("*, users(nama, id)").eq("id", asset_id).execute()
    if not asset.data:
        raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
    a             = asset.data[0]
    uploader_nama = a.get("users", {}).get("nama", "—") if a.get("users") else "—"
    uploader_id   = a.get("users", {}).get("id") if a.get("users") else None
    a.pop("users", None)
    a["uploader"] = uploader_nama

    if uploader_id:
        ud = get_user_division(uploader_id)
        a["uploader_divisi"] = ud["divisions"]["nama"] if ud and ud.get("divisions") else "—"
    else:
        a["uploader_divisi"] = "—"

    asset_tags = supabase.table("asset_tags").select("*, tags(nama), users(nama)").eq("asset_id", asset_id).execute()
    tags = [{
        "nama":      at["tags"]["nama"],
        "sumber":    at["sumber"],
        "added_by":  at.get("users", {}).get("nama") if at.get("users") else None
    } for at in asset_tags.data]
    return {"asset": a, "tags": tags}

@app.post("/assets/{asset_id}/log-download")
def log_download(asset_id: str, data: DownloadLogInput):
    asset = supabase.table("assets").select("nama_file").eq("id", asset_id).execute()
    nama_file = asset.data[0]["nama_file"] if asset.data else "—"
    log_activity("download", data.user_id, asset_id=asset_id, detail={"nama_file": nama_file})
    return {"message": "Download dicatat"}

@app.delete("/assets/{asset_id}")
def delete_asset(asset_id: str, user_id: Optional[str] = None):
    try:
        asset = supabase.table("assets").select("*, divisions(nama)").eq("id", asset_id).execute()
        if not asset.data:
            raise HTTPException(status_code=404, detail="Aset tidak ditemukan")

        a = asset.data[0]

        if user_id and a.get("user_id") != user_id and not is_manager(user_id):
            raise HTTPException(
                status_code=403,
                detail="Hanya uploader atau Manager yang bisa menghapus aset ini"
            )

        # Simpan informasi penting sebagai audit log sebelum row asset dihapus.
        log_activity("delete", user_id, asset_id=None, detail={
            "nama_file": a.get("nama_file"),
            "tipe_file": a.get("tipe_file"),
            "ukuran": a.get("ukuran"),
            "divisi": a.get("divisions", {}).get("nama") if a.get("divisions") else None
        })

        # Bersihkan semua relasi yang secara langsung menunjuk ke asset.
        supabase.table("asset_tags").delete().eq("asset_id", asset_id).execute()
        supabase.table("asset_folders").delete().eq("asset_id", asset_id).execute()
        supabase.table("asset_shares").delete().eq("asset_id", asset_id).execute()
        supabase.table("asset_permissions").delete().eq("asset_id", asset_id).execute()

        # Activity log harus tetap disimpan untuk audit, tetapi foreign key
        # ke asset yang akan dihapus perlu dilepaskan terlebih dahulu.
        supabase.table("activity_log").update({
            "asset_id": None
        }).eq("asset_id", asset_id).execute()

        # Setelah seluruh foreign key aman, baru hapus row asset utama.
        supabase.table("assets").delete().eq("id", asset_id).execute()

        # Hapus file storage secara best-effort. URL asset lama bisa memiliki
        # format berbeda, jadi kegagalan storage tidak boleh membatalkan delete DB.
        url = a.get("url") or ""

        try:
            marker = "/object/public/assets/"

            if marker in url:
                path = url.split(marker, 1)[1]
                supabase.storage.from_("assets").remove([path])
            else:
                print(
                    f"Storage path tidak dikenali asset_id={asset_id}, url={url}"
                )

        except Exception as storage_error:
            print(
                f"Gagal menghapus storage asset_id={asset_id}: {storage_error}"
            )

        return {"message": "Aset berhasil dihapus"}

    except HTTPException:
        raise

    except Exception as e:
        print(f"DELETE asset error asset_id={asset_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Gagal menghapus aset: {str(e)}"
        )

@app.post("/assets/{asset_id}/tags")
def tambah_tag(asset_id: str, data: TagInput):
    # Pisah input berdasarkan koma dan/atau spasi, buang yang kosong, hilangkan duplikat.
    raw_tokens = re.split(r"[,\s]+", data.nama_tag.strip())
    nama_list  = []
    seen       = set()
    for t in raw_tokens:
        t = t.strip().lower()
        if t and t not in seen:
            seen.add(t)
            nama_list.append(t)

    if not nama_list:
        raise HTTPException(status_code=400, detail="Tag tidak boleh kosong")

    added = []
    for nama_tag in nama_list:
        existing_tag = supabase.table("tags").select("id").eq("nama", nama_tag).execute()
        if existing_tag.data:
            tag_id = existing_tag.data[0]["id"]
        else:
            result = supabase.table("tags").insert({"nama": nama_tag}).execute()
            tag_id = result.data[0]["id"]

        already = supabase.table("asset_tags").select("id").eq("asset_id", asset_id).eq("tag_id", tag_id).execute()
        if already.data:
            continue

        supabase.table("asset_tags").insert({
            "asset_id":   asset_id,
            "tag_id":     tag_id,
            "sumber":     data.sumber,
            "created_by": data.user_id
        }).execute()
        added.append(nama_tag)

    # Catat sebagai satu aktivitas (bukan per-tag), supaya laporan
    # aktivitas menampilkan "User X menambahkan N tag pada file Y".
    if added and data.user_id:
        supabase.table("activity_log").insert({
            "type":     "tag_added",
            "user_id":  data.user_id,
            "asset_id": asset_id,
            "detail":   {"tags": added, "count": len(added), "sumber": data.sumber}
        }).execute()

    assigned_folders = []
    if added:
        try:
            assigned_folders = auto_assign_asset_from_current_tags(asset_id)
        except Exception as assign_error:
            # Tag tetap dianggap berhasil meskipun evaluasi Smart Folder gagal.
            print(f"Gagal auto-assign setelah tambah tag asset_id={asset_id}: {assign_error}")

    if not added:
        return {
            "message": "Semua tag sudah ada sebelumnya",
            "added": [],
            "assigned_folders": []
        }

    return {
        "message": f"{len(added)} tag ditambahkan",
        "added": added,
        "assigned_folders": assigned_folders
    }

@app.delete("/assets/{asset_id}/tags/{nama_tag}")
def hapus_tag(asset_id: str, nama_tag: str, user_id: Optional[str] = None):
    tag = supabase.table("tags").select("id").eq("nama", nama_tag).execute()
    if not tag.data:
        raise HTTPException(status_code=404, detail="Tag tidak ditemukan")
    tag_id = tag.data[0]["id"]
    supabase.table("asset_tags").delete().eq("asset_id", asset_id).eq("tag_id", tag_id).execute()
    log_activity("tag_removed", user_id, asset_id=asset_id, detail={"tag": nama_tag})
    return {"message": "Tag dihapus"}

@app.post("/folders/{folder_id}/add-asset")
def add_asset_to_folder(folder_id: str, asset_id: str, user_id: str):
    folder = supabase.table("folders").select("*").eq("id", folder_id).execute()
    if not folder.data:
        raise HTTPException(status_code=404, detail="Folder tidak ditemukan")
    f = folder.data[0]
    if not can_access_folder(user_id, f):
        raise HTTPException(status_code=403, detail="Tidak punya akses ke folder ini")
    already = supabase.table("asset_folders").select("id").eq("asset_id", asset_id).eq("folder_id", folder_id).execute()
    if not already.data:
        supabase.table("asset_folders").insert({
            "asset_id":  asset_id,
            "folder_id": folder_id
        }).execute()
    return {"message": "Aset ditambahkan ke folder"}

@app.delete("/folders/{folder_id}/assets/{asset_id}")
def remove_asset_from_folder(folder_id: str, asset_id: str, user_id: str):
    folder = supabase.table("folders").select("*").eq("id", folder_id).execute()
    if not folder.data:
        raise HTTPException(status_code=404, detail="Folder tidak ditemukan")
    f = folder.data[0]
    if not can_access_folder(user_id, f):
        raise HTTPException(status_code=403, detail="Tidak punya akses ke folder ini")
    supabase.table("asset_folders").delete().eq("asset_id", asset_id).eq("folder_id", folder_id).execute()

    asset = supabase.table("assets").select("nama_file").eq("id", asset_id).execute()
    nama_file = asset.data[0]["nama_file"] if asset.data else "—"
    log_activity("remove_from_folder", user_id, asset_id=asset_id, detail={
        "nama_file": nama_file,
        "folder":    f.get("nama")
    })

    return {"message": "Aset dikeluarkan dari folder"}

@app.post("/assets/{asset_id}/move-folder")
def move_asset_to_folder(asset_id: str, data: MoveAssetInput):
    target = supabase.table("folders").select("*").eq("id", data.to_folder_id).execute()
    if not target.data:
        raise HTTPException(status_code=404, detail="Folder tujuan tidak ditemukan")
    if not can_access_folder(data.user_id, target.data[0]):
        raise HTTPException(status_code=403, detail="Tidak punya akses ke folder tujuan")

    from_folder_nama = None
    if data.from_folder_id and data.from_folder_id != data.to_folder_id:
        source = supabase.table("folders").select("*").eq("id", data.from_folder_id).execute()
        if source.data:
            if not can_access_folder(data.user_id, source.data[0]):
                raise HTTPException(status_code=403, detail="Tidak punya akses ke folder asal")
            from_folder_nama = source.data[0].get("nama")
        supabase.table("asset_folders").delete().eq("asset_id", asset_id).eq("folder_id", data.from_folder_id).execute()

    already = supabase.table("asset_folders").select("id").eq("asset_id", asset_id).eq("folder_id", data.to_folder_id).execute()
    if not already.data:
        supabase.table("asset_folders").insert({
            "asset_id":  asset_id,
            "folder_id": data.to_folder_id
        }).execute()

    asset = supabase.table("assets").select("nama_file").eq("id", asset_id).execute()
    nama_file = asset.data[0]["nama_file"] if asset.data else "—"
    log_activity("move", data.user_id, asset_id=asset_id, detail={
        "nama_file":   nama_file,
        "to_folder":   target.data[0].get("nama"),
        "from_folder": from_folder_nama
    })

    return {"message": "Aset dipindahkan ke folder baru"}

# ── Tags ─────────────────────────────────────────────
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
    ud = get_user_division(user_id)
    if ud and ud["division_id"] == MANAGER_ID:
        result = supabase.table("asset_tags").select("tag_id").execute()
    else:
        assets = supabase.table("assets").select("id").eq("user_id", user_id).execute()
        asset_ids = [a["id"] for a in assets.data]
        if not asset_ids:
            return {"count": 0}
        result = supabase.table("asset_tags").select("tag_id").in_("asset_id", asset_ids).execute()
    unique_tags = set(at["tag_id"] for at in result.data)
    return {"count": len(unique_tags)}

@app.get("/tags/top")
def top_tags(user_id: str, limit: int = 7):
    ud = get_user_division(user_id)
    if ud and ud["division_id"] == MANAGER_ID:
        result = supabase.table("asset_tags").select("tag_id, tags(nama)").execute()
    else:
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

# ── Search ───────────────────────────────────────────
@app.get("/search/by-tag")
def get_assets_by_tag(user_id: str, tag: str):
    """Filter tag exact yang mengikuti permission Gallery, bukan hanya uploader."""
    tag_name = (tag or "").strip().lower()
    if not tag_name:
        return {"asset_ids": []}

    tag_result = supabase.table("tags").select("id").eq("nama", tag_name).execute()
    if not tag_result.data:
        return {"asset_ids": []}

    tag_id = tag_result.data[0]["id"]
    at_result = supabase.table("asset_tags").select("asset_id").eq("tag_id", tag_id).execute()
    candidate_ids = list({
        row["asset_id"]
        for row in (at_result.data or [])
        if row.get("asset_id")
    })

    if not candidate_ids:
        return {"asset_ids": []}

    asset_result = supabase.table("assets").select("*").in_("id", candidate_ids).execute()

    allowed_ids = [
        asset["id"]
        for asset in (asset_result.data or [])
        if can_access_asset(user_id, asset)
    ]

    return {"asset_ids": allowed_ids}


@app.get("/search/gallery")
def search_gallery_tags(user_id: str, q: str):
    """Cari tag secara partial untuk search bar Gallery.

    Endpoint ini hanya mengembalikan ID aset yang tag-nya cocok dan user memang
    boleh akses. Frontend tetap menggabungkan hasil ini dengan pencarian nama
    file/folder dan membatasi hasil ke lokasi Gallery yang sedang dibuka.
    """
    query = (q or "").strip().lower()
    if not query:
        return {"asset_ids": []}

    # Partial match, contoh "file" akan cocok dengan "file-besar".
    tag_result = (
        supabase.table("tags")
        .select("id, nama")
        .ilike("nama", f"%{query}%")
        .execute()
    )

    tag_ids = [
        row["id"]
        for row in (tag_result.data or [])
        if row.get("id")
    ]

    if not tag_ids:
        return {"asset_ids": []}

    at_result = (
        supabase.table("asset_tags")
        .select("asset_id")
        .in_("tag_id", tag_ids)
        .execute()
    )

    candidate_ids = list({
        row["asset_id"]
        for row in (at_result.data or [])
        if row.get("asset_id")
    })

    if not candidate_ids:
        return {"asset_ids": []}

    asset_result = (
        supabase.table("assets")
        .select("*")
        .in_("id", candidate_ids)
        .execute()
    )

    allowed_ids = [
        asset["id"]
        for asset in (asset_result.data or [])
        if can_access_asset(user_id, asset)
    ]

    return {"asset_ids": allowed_ids}

# ── Stats ────────────────────────────────────────────
@app.get("/stats/tagging")
def stats_tagging(user_id: str):
    ud = get_user_division(user_id)
    if ud and ud["division_id"] == MANAGER_ID:
        result = supabase.table("asset_tags").select("sumber").execute()
    else:
        assets = supabase.table("assets").select("id").eq("user_id", user_id).execute()
        asset_ids = [a["id"] for a in assets.data]
        if not asset_ids:
            return {"total": 0, "nama_file": 0, "metadata": 0, "ai": 0, "manual": 0,
                    "pct_nama_file": 0, "pct_metadata": 0, "pct_ai": 0, "pct_manual": 0}
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

# ── Folders ──────────────────────────────────────────
def _get_folder(folder_id: str):
    result = supabase.table("folders").select("*").eq("id", folder_id).execute()
    return result.data[0] if result.data else None


def _folder_divisions(folder: dict) -> list:
    """Ambil daftar divisi efektif folder. Folder lama tetap didukung."""
    if not folder:
        return []
    ids = get_folder_division_ids(folder["id"])
    if not ids and folder.get("division_id"):
        ids = [folder["division_id"]]
    return list(dict.fromkeys(ids))


def _assert_folder_manageable(user_id: str, folder: dict):
    if not folder:
        raise HTTPException(status_code=404, detail="Folder tidak ditemukan")
    if folder.get("type") != "smart":
        raise HTTPException(status_code=400, detail="Hanya Smart Folder yang bisa diubah")
    if folder.get("user_id") != user_id and not is_manager(user_id):
        raise HTTPException(status_code=403, detail="Tidak punya izin mengubah folder ini")


def _is_descendant(candidate_parent_id: str, folder_id: str) -> bool:
    """True jika candidate_parent berada di bawah folder_id (mencegah cycle)."""
    current_id = candidate_parent_id
    seen = set()
    while current_id:
        if current_id == folder_id:
            return True
        if current_id in seen:
            return True
        seen.add(current_id)
        row = _get_folder(current_id)
        if not row:
            return False
        current_id = row.get("parent_id")
    return False


def _set_folder_division_rows(folder_id: str, division_ids: list):
    division_ids = [d for d in dict.fromkeys(division_ids or []) if d]
    supabase.table("folder_divisions").delete().eq("folder_id", folder_id).execute()
    for div_id in division_ids:
        supabase.table("folder_divisions").insert({
            "folder_id": folder_id,
            "division_id": div_id
        }).execute()
    if division_ids:
        supabase.table("folders").update({"division_id": division_ids[0]}).eq("id", folder_id).execute()


@app.post("/folders")
def buat_folder(data: FolderInput):
    payload = {"nama": data.nama, "type": "smart", "user_id": data.user_id}
    if data.parent_id:
        payload["parent_id"] = data.parent_id
    result = supabase.table("folders").insert(payload).execute()
    return {"message": "Folder dibuat", "folder": result.data[0]}


@app.post("/folders/smart")
def buat_smart_folder(data: FolderInput):
    nama = (data.nama or "").strip()
    if not nama:
        raise HTTPException(status_code=400, detail="Nama folder tidak boleh kosong")

    ud = get_user_division(data.user_id)
    if not ud:
        raise HTTPException(status_code=400, detail="User tidak memiliki divisi")
    own_division_id = ud["division_id"]

    parent = None
    if data.parent_id:
        parent = _get_folder(data.parent_id)
        if not parent:
            raise HTTPException(status_code=404, detail="Folder induk tidak ditemukan")
        if parent.get("type") not in ("system", "smart"):
            raise HTTPException(status_code=400, detail="Smart Folder hanya bisa diletakkan di Folder Divisi atau Smart Folder")
        if not can_access_folder(data.user_id, parent):
            raise HTTPException(status_code=403, detail="Tidak punya akses ke folder induk")

    if parent:
        # Child mewarisi divisi parent agar hierarchy tidak menghasilkan akses yang bocor.
        target_ids = _folder_divisions(parent)
        if not target_ids and parent.get("division_id"):
            target_ids = [parent["division_id"]]
    else:
        target_ids = list(data.division_ids) if data.division_ids else []
        if data.target_division_id and data.target_division_id not in target_ids:
            target_ids.append(data.target_division_id)
        non_own = [d for d in target_ids if d != own_division_id]
        if non_own and not is_manager(data.user_id):
            raise HTTPException(status_code=403, detail="Hanya Manager yang bisa membuat Smart Folder untuk divisi lain")
        if not target_ids:
            target_ids = [own_division_id]

    if not target_ids:
        raise HTTPException(status_code=400, detail="Folder harus memiliki minimal satu divisi")

    result = supabase.table("folders").insert({
        "nama": nama,
        "type": "smart",
        "division_id": target_ids[0],
        "user_id": data.user_id,
        "parent_id": data.parent_id
    }).execute()
    folder = result.data[0]
    _set_folder_division_rows(folder["id"], target_ids)
    return {"message": "Smart folder dibuat", "folder": folder}


@app.put("/folders/{folder_id}/move")
def move_folder(folder_id: str, data: MoveFolderInput):
    folder = _get_folder(folder_id)
    _assert_folder_manageable(data.user_id, folder)

    target_divisions = _folder_divisions(folder)
    new_parent = None
    if data.parent_id:
        if data.parent_id == folder_id:
            raise HTTPException(status_code=400, detail="Folder tidak bisa dipindahkan ke dirinya sendiri")
        if _is_descendant(data.parent_id, folder_id):
            raise HTTPException(status_code=400, detail="Folder tidak bisa dipindahkan ke subfoldernya sendiri")
        new_parent = _get_folder(data.parent_id)
        if not new_parent:
            raise HTTPException(status_code=404, detail="Folder tujuan tidak ditemukan")
        if new_parent.get("type") not in ("system", "smart"):
            raise HTTPException(status_code=400, detail="Tujuan harus Folder Divisi atau Smart Folder")
        if not can_access_folder(data.user_id, new_parent):
            raise HTTPException(status_code=403, detail="Tidak punya akses ke folder tujuan")
        target_divisions = _folder_divisions(new_parent)

    supabase.table("folders").update({"parent_id": data.parent_id}).eq("id", folder_id).execute()
    if target_divisions:
        _set_folder_division_rows(folder_id, target_divisions)
    return {"message": "Folder berhasil dipindahkan"}


@app.post("/folders/{folder_id}/divisions")
def set_folder_divisions(folder_id: str, data: FolderDivisionInput):
    folder = _get_folder(folder_id)
    _assert_folder_manageable(data.granted_by, folder)
    if folder.get("parent_id"):
        raise HTTPException(status_code=400, detail="Akses child Smart Folder mengikuti folder induknya")
    if not data.division_ids:
        raise HTTPException(status_code=400, detail="Pilih minimal satu divisi")
    if not is_manager(data.granted_by):
        ud = get_user_division(data.granted_by)
        own = ud["division_id"] if ud else None
        if any(d != own for d in data.division_ids):
            raise HTTPException(status_code=403, detail="Hanya Manager yang bisa memberi akses lintas divisi")
    _set_folder_division_rows(folder_id, data.division_ids)
    return {"message": "Akses divisi folder diperbarui"}


@app.get("/folders/{folder_id}/divisions")
def get_folder_divisions_endpoint(folder_id: str):
    try:
        access_result = (
            supabase.table("folder_divisions")
            .select("division_id")
            .eq("folder_id", folder_id)
            .execute()
        )

        division_ids = [
            row["division_id"]
            for row in (access_result.data or [])
            if row.get("division_id")
        ]

        if not division_ids:
            return {"divisions": []}

        divisions_result = (
            supabase.table("divisions")
            .select("id, nama")
            .in_("id", division_ids)
            .execute()
        )

        return {"divisions": divisions_result.data or []}

    except Exception as e:
        print(f"GET folder divisions error folder_id={folder_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Gagal mengambil akses divisi: {str(e)}"
        )


@app.get("/folders")
def get_folders(user_id: str):
    ud = get_user_division(user_id)
    division_id = ud["division_id"] if ud else None

    access_result = supabase.table("folder_access").select(
        "folder_id, granted_by"
    ).eq("user_id", user_id).execute()
    access_by_folder = {row["folder_id"]: row.get("granted_by") for row in access_result.data}

    div_folder_ids = []
    if division_id:
        div_access_result = supabase.table("folder_divisions").select("folder_id").eq(
            "division_id", division_id
        ).execute()
        div_folder_ids = [row["folder_id"] for row in div_access_result.data]

    if division_id == MANAGER_ID:
        result = supabase.table("folders").select(
            "*, users(nama), divisions(nama)"
        ).order("type").order("nama").execute()
        folder_rows = list(result.data)
    else:
        if division_id:
            result = supabase.table("folders").select(
                "*, users(nama), divisions(nama)"
            ).or_(
                f"type.eq.shared,and(type.eq.system,division_id.eq.{division_id}),and(type.eq.smart,division_id.eq.{division_id})"
            ).order("type").order("nama").execute()
            folder_rows = list(result.data)
        else:
            result = supabase.table("folders").select(
                "*, users(nama), divisions(nama)"
            ).eq("type", "shared").order("nama").execute()
            folder_rows = list(result.data)

        existing_ids = {folder["id"] for folder in folder_rows}
        extra_ids = [
            fid for fid in set(list(access_by_folder.keys()) + div_folder_ids)
            if fid not in existing_ids
        ]
        if extra_ids:
            shared_rows = supabase.table("folders").select(
                "*, users(nama), divisions(nama)"
            ).in_("id", extra_ids).execute()
            folder_rows.extend(shared_rows.data)

    granted_by_ids = {uid for uid in access_by_folder.values() if uid}
    granted_by_map = {}
    if granted_by_ids:
        users_result = supabase.table("users").select("id, nama").in_("id", list(granted_by_ids)).execute()
        granted_by_map = {row["id"]: row["nama"] for row in users_result.data}

    folders = []
    for folder in folder_rows:
        owner_name = folder.get("users", {}).get("nama", "—") if folder.get("users") else "—"
        shared_via_user = folder["id"] in access_by_folder and folder.get("user_id") != user_id
        shared_via_division = bool(
            division_id and folder["id"] in div_folder_ids
            and folder.get("division_id") != division_id
            and folder.get("user_id") != user_id
        )
        shared_to_me = shared_via_user or shared_via_division
        granted_by_id = access_by_folder.get(folder["id"])
        shared_by = granted_by_map.get(granted_by_id) if granted_by_id else None
        if shared_to_me and not shared_by:
            shared_by = owner_name

        item = {
            **folder,
            "owner": owner_name,
            "div_nama": folder.get("divisions", {}).get("nama", "") if folder.get("divisions") else "",
            "shared_to_me": bool(shared_to_me),
            "shared_by": shared_by
        }
        item.pop("users", None)
        item.pop("divisions", None)
        folders.append(item)

    return {"folders": folders}


@app.delete("/folders/{folder_id}")
def hapus_folder(folder_id: str, user_id: Optional[str] = None):
    folder = _get_folder(folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder tidak ditemukan")
    if user_id:
        _assert_folder_manageable(user_id, folder)
    children = supabase.table("folders").select("id").eq("parent_id", folder_id).execute()
    if children.data:
        raise HTTPException(status_code=400, detail="Folder masih memiliki subfolder. Pindahkan atau hapus subfolder terlebih dahulu.")
    supabase.table("asset_folders").delete().eq("folder_id", folder_id).execute()
    supabase.table("folder_rules").delete().eq("folder_id", folder_id).execute()
    supabase.table("folder_access").delete().eq("folder_id", folder_id).execute()
    supabase.table("folder_divisions").delete().eq("folder_id", folder_id).execute()
    supabase.table("folders").delete().eq("id", folder_id).execute()
    return {"message": "Folder dihapus"}


# ── Folder Access (share smart folder ke user tertentu) ──
@app.post("/folders/{folder_id}/access")
def set_folder_access(folder_id: str, data: FolderAccessInput):
    folder = _get_folder(folder_id)
    _assert_folder_manageable(data.granted_by, folder)
    existing = supabase.table("folder_access").select("user_id").eq("folder_id", folder_id).execute()
    existing_ids = {e["user_id"] for e in existing.data}
    for uid in data.user_ids:
        if uid not in existing_ids:
            supabase.table("folder_access").insert({
                "folder_id": folder_id,
                "user_id": uid,
                "granted_by": data.granted_by
            }).execute()
    return {"message": "Akses folder diperbarui"}


@app.post("/folders/{folder_id}/share")
def share_folder_to_division(folder_id: str, data: FolderShareInput):
    folder = _get_folder(folder_id)
    _assert_folder_manageable(data.from_user_id, folder)
    division_result = supabase.table("divisions").select("id, nama").eq("id", data.to_division_id).execute()
    if not division_result.data:
        raise HTTPException(status_code=404, detail="Divisi tujuan tidak ditemukan")

    existing_division = supabase.table("folder_divisions").select("id").eq(
        "folder_id", folder_id
    ).eq("division_id", data.to_division_id).execute()
    if not existing_division.data:
        supabase.table("folder_divisions").insert({
            "folder_id": folder_id,
            "division_id": data.to_division_id
        }).execute()

    target_users = supabase.table("user_divisions").select("user_id").eq(
        "division_id", data.to_division_id
    ).execute()
    shared_count = 0
    for row in target_users.data:
        target_user_id = row["user_id"]
        if target_user_id == data.from_user_id:
            continue
        existing_access = supabase.table("folder_access").select("id").eq(
            "folder_id", folder_id
        ).eq("user_id", target_user_id).execute()
        if existing_access.data:
            supabase.table("folder_access").update({
                "granted_by": data.from_user_id
            }).eq("id", existing_access.data[0]["id"]).execute()
        else:
            supabase.table("folder_access").insert({
                "folder_id": folder_id,
                "user_id": target_user_id,
                "granted_by": data.from_user_id
            }).execute()
        shared_count += 1

    return {
        "message": "Folder berhasil dibagikan ke divisi",
        "division": division_result.data[0],
        "shared_to_users": shared_count
    }


@app.get("/folders/{folder_id}/access")
def get_folder_access(folder_id: str):
    try:
        access_result = (
            supabase.table("folder_access")
            .select("user_id, granted_by")
            .eq("folder_id", folder_id)
            .execute()
        )

        rows = access_result.data or []

        if not rows:
            return {"users": []}

        user_ids = list({
            row["user_id"]
            for row in rows
            if row.get("user_id")
        })

        if not user_ids:
            return {"users": []}

        users_result = (
            supabase.table("users")
            .select("id, nama, email")
            .in_("id", user_ids)
            .execute()
        )

        users_by_id = {
            user["id"]: user
            for user in (users_result.data or [])
        }

        users = []
        for row in rows:
            user = users_by_id.get(row.get("user_id"))
            if not user:
                continue

            users.append({
                **user,
                "granted_by": row.get("granted_by")
            })

        return {"users": users}

    except Exception as e:
        print(f"GET folder access error folder_id={folder_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Gagal mengambil akses user: {str(e)}"
        )


@app.delete("/folders/{folder_id}/access/{user_id}")
def hapus_folder_access(folder_id: str, user_id: str):
    supabase.table("folder_access").delete().eq("folder_id", folder_id).eq("user_id", user_id).execute()
    return {"message": "Akses dicabut"}


def _folder_primary_scope_divisions(folder: dict) -> list:
    """Divisi sumber aset untuk Smart Folder.

    Akses share ke divisi lain tidak membuat Smart Folder menarik aset dari divisi
    penerima share. Karena itu division_id utama diprioritaskan.
    """
    if folder.get("division_id"):
        return [folder["division_id"]]
    return _folder_divisions(folder)


def _folder_descendant_ids(folder_id: str) -> set:
    descendants = set()
    queue = [folder_id]

    while queue:
        parent_id = queue.pop(0)
        result = supabase.table("folders").select("id").eq("parent_id", parent_id).execute()
        for row in (result.data or []):
            child_id = row.get("id")
            if child_id and child_id not in descendants:
                descendants.add(child_id)
                queue.append(child_id)

    return descendants


def _matching_assets_for_smart_folder(folder: dict, user_id: str) -> dict:
    """Preview aset lama yang cocok dengan rules Smart Folder, tanpa memasukkannya."""
    _assert_folder_manageable(user_id, folder)

    rule_result = supabase.table("folder_rules").select("keyword").eq(
        "folder_id", folder["id"]
    ).execute()
    keywords = list(dict.fromkeys([
        str(row.get("keyword") or "").strip().lower()
        for row in (rule_result.data or [])
        if str(row.get("keyword") or "").strip()
    ]))

    if not keywords:
        return {
            "assets": [],
            "keywords": [],
            "already_assigned_count": 0,
            "scanned_count": 0
        }

    scope_divisions = _folder_primary_scope_divisions(folder)
    if not scope_divisions:
        return {
            "assets": [],
            "keywords": keywords,
            "already_assigned_count": 0,
            "scanned_count": 0
        }

    asset_result = supabase.table("assets").select(
        "*, users(nama)"
    ).in_("division_id", scope_divisions).order("created_at", desc=True).execute()

    scoped_assets = [
        asset
        for asset in (asset_result.data or [])
        if can_access_asset(user_id, asset)
    ]

    if not scoped_assets:
        return {
            "assets": [],
            "keywords": keywords,
            "already_assigned_count": 0,
            "scanned_count": 0
        }

    asset_ids = [asset["id"] for asset in scoped_assets]
    tag_result = supabase.table("asset_tags").select(
        "asset_id, sumber, tags(nama)"
    ).in_("asset_id", asset_ids).execute()

    tags_by_asset = {asset_id: [] for asset_id in asset_ids}
    for row in (tag_result.data or []):
        tag_data = row.get("tags") or {}
        tag_name = str(tag_data.get("nama") or "").strip().lower()
        if not tag_name:
            continue
        tags_by_asset.setdefault(row.get("asset_id"), []).append({
            "nama": tag_name,
            "sumber": row.get("sumber") or ""
        })

    hierarchy_ids = {folder["id"]} | _folder_descendant_ids(folder["id"])
    assigned_result = supabase.table("asset_folders").select(
        "asset_id, folder_id"
    ).in_("folder_id", list(hierarchy_ids)).execute()
    hierarchy_asset_ids = {
        row.get("asset_id")
        for row in (assigned_result.data or [])
        if row.get("asset_id")
    }

    candidates = []
    already_assigned_count = 0

    for asset in scoped_assets:
        tags = tags_by_asset.get(asset["id"], [])
        matched_tags = sorted({
            tag["nama"]
            for tag in tags
            if any(keyword in tag["nama"] for keyword in keywords)
        })
        if not matched_tags:
            continue

        matched_rules = sorted({
            keyword
            for keyword in keywords
            if any(keyword in tag["nama"] for tag in tags)
        })

        if asset["id"] in hierarchy_asset_ids:
            already_assigned_count += 1
            continue

        uploader_data = asset.get("users") or {}
        item = {**asset}
        item.pop("users", None)
        item["uploader"] = uploader_data.get("nama", "—")
        item["matching_tags"] = matched_tags
        item["matching_rules"] = matched_rules
        candidates.append(item)

    return {
        "assets": candidates,
        "keywords": keywords,
        "already_assigned_count": already_assigned_count,
        "scanned_count": len(scoped_assets)
    }


@app.get("/folders/{folder_id}/matching-assets")
def get_folder_matching_assets(folder_id: str, user_id: str):
    folder = _get_folder(folder_id)
    return _matching_assets_for_smart_folder(folder, user_id)


@app.post("/folders/{folder_id}/assign-matching-assets")
def assign_matching_assets(folder_id: str, data: AssignMatchingAssetsInput):
    folder = _get_folder(folder_id)
    preview = _matching_assets_for_smart_folder(folder, data.user_id)

    allowed = {
        asset["id"]: asset
        for asset in preview.get("assets", [])
    }
    requested_ids = list(dict.fromkeys([
        str(asset_id)
        for asset_id in (data.asset_ids or [])
        if asset_id
    ]))

    valid_ids = [asset_id for asset_id in requested_ids if asset_id in allowed]
    skipped_ids = [asset_id for asset_id in requested_ids if asset_id not in allowed]

    assigned_ids = []
    for asset_id in valid_ids:
        added = commit_folder_assignments(
            asset_id,
            [folder_id],
            prune_ancestors=True
        )
        if folder_id in added:
            assigned_ids.append(asset_id)

    return {
        "message": f"{len(assigned_ids)} aset dimasukkan ke Smart Folder",
        "assigned_asset_ids": assigned_ids,
        "assigned_count": len(assigned_ids),
        "skipped_asset_ids": skipped_ids,
        "skipped_count": len(skipped_ids)
    }


@app.post("/folders/rules")
def buat_rule(data: RuleInput):
    keyword = (data.keyword or "").strip().lower()
    if not keyword:
        raise HTTPException(status_code=400, detail="Keyword tidak boleh kosong")
    folder = _get_folder(data.folder_id)
    _assert_folder_manageable(data.user_id, folder)
    existing = supabase.table("folder_rules").select("id").eq("folder_id", data.folder_id).eq("keyword", keyword).execute()
    if existing.data:
        return {"message": "Rule sudah ada", "rule": existing.data[0], "duplicate": True}
    result = supabase.table("folder_rules").insert({
        "user_id": data.user_id,
        "keyword": keyword,
        "folder_id": data.folder_id
    }).execute()
    return {"message": "Rule ditambahkan", "rule": result.data[0]}


@app.post("/folders/rules/batch")
def buat_rules_batch(data: RuleBatchInput):
    folder = _get_folder(data.folder_id)
    _assert_folder_manageable(data.user_id, folder)
    keywords = []
    seen = set()
    for raw in data.keywords or []:
        keyword = str(raw).strip().lower()
        if keyword and keyword not in seen:
            seen.add(keyword)
            keywords.append(keyword)
    if not keywords:
        raise HTTPException(status_code=400, detail="Masukkan minimal satu keyword")

    current = supabase.table("folder_rules").select("keyword").eq("folder_id", data.folder_id).execute()
    existing = {str(r.get("keyword", "")).lower() for r in current.data}
    inserted = []
    skipped = []
    for keyword in keywords:
        if keyword in existing:
            skipped.append(keyword)
            continue
        result = supabase.table("folder_rules").insert({
            "user_id": data.user_id,
            "keyword": keyword,
            "folder_id": data.folder_id
        }).execute()
        inserted.append(result.data[0])
        existing.add(keyword)
    return {"message": f"{len(inserted)} rule ditambahkan", "rules": inserted, "skipped": skipped}


@app.get("/folders/rules")
def get_rules(user_id: str, folder_id: Optional[str] = None):
    query = supabase.table("folder_rules").select("*, folders(nama)")
    if folder_id:
        query = query.eq("folder_id", folder_id)
    result = query.execute()
    return {"rules": result.data}


@app.delete("/folders/rules/{rule_id}")
def hapus_rule(rule_id: str):
    supabase.table("folder_rules").delete().eq("id", rule_id).execute()
    return {"message": "Rule dihapus"}


@app.get("/folders/{folder_id}/assets")
def get_assets_by_folder(folder_id: str, user_id: Optional[str] = None):
    if user_id:
        folder = _get_folder(folder_id)
        if not folder:
            raise HTTPException(status_code=404, detail="Folder tidak ditemukan")
        if not can_access_folder(user_id, folder):
            raise HTTPException(status_code=403, detail="Tidak punya akses ke folder ini")

    result = supabase.table("asset_folders").select(
        "asset_id, assets(*)"
    ).eq("folder_id", folder_id).execute()

    raw_assets = [
        row["assets"]
        for row in (result.data or [])
        if row.get("assets")
    ]

    if not raw_assets:
        return {"assets": []}

    uploader_ids = list({
        asset.get("user_id")
        for asset in raw_assets
        if asset.get("user_id")
    })

    uploader_map = {}
    if uploader_ids:
        users_result = supabase.table("users").select(
            "id, nama"
        ).in_("id", uploader_ids).execute()

        uploader_map = {
            row["id"]: row.get("nama", "—")
            for row in (users_result.data or [])
        }

    assets = []
    for asset in raw_assets:
        item = {
            **asset,
            "uploader": uploader_map.get(asset.get("user_id"), "—")
        }
        assets.append(item)

    return {"assets": assets}

# ── Divisions ────────────────────────────────────────
@app.get("/divisions")
def get_divisions():
    result = supabase.table("divisions").select("*").order("nama").execute()
    return {"divisions": result.data}

@app.post("/divisions/assign")
def assign_division(user_id: str, division_id: str):
    existing = supabase.table("user_divisions").select("id").eq("user_id", user_id).execute()
    if existing.data:
        supabase.table("user_divisions").update({"division_id": division_id}).eq("user_id", user_id).execute()
    else:
        supabase.table("user_divisions").insert({"user_id": user_id, "division_id": division_id}).execute()
    return {"message": "Divisi berhasil diassign"}

@app.get("/divisions/user/{user_id}")
def get_user_division_endpoint(user_id: str):
    result = supabase.table("user_divisions").select("*, divisions(*)").eq("user_id", user_id).execute()
    if not result.data:
        return {"division": None}
    return {"division": result.data[0]}

@app.get("/divisions/users")
def get_all_users_with_division():
    users = supabase.table("users").select("id, nama, email, created_at").execute()
    result = []
    for u in users.data:
        ud = supabase.table("user_divisions").select("*, divisions(nama)").eq("user_id", u["id"]).execute()
        division = ud.data[0] if ud.data else None
        result.append({**u, "division": division})
    return {"users": result}

# ── Profile & Password ───────────────────────────────
@app.put("/profile/{user_id}")
def update_profile(user_id: str, data: UpdateProfileInput):
    if not data.nama.strip():
        raise HTTPException(status_code=400, detail="Nama tidak boleh kosong")
    supabase.table("users").update({"nama": data.nama.strip()}).eq("id", user_id).execute()
    return {"message": "Nama berhasil diupdate"}

@app.put("/profile/{user_id}/password")
def update_password(user_id: str, data: UpdatePasswordInput):
    user = supabase.table("users").select("password").eq("id", user_id).execute()
    if not user.data:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")
    if not bcrypt.checkpw(data.password_lama.encode(), user.data[0]["password"].encode()):
        raise HTTPException(status_code=400, detail="Password lama tidak sesuai")
    if len(data.password_baru) < 6:
        raise HTTPException(status_code=400, detail="Password baru minimal 6 karakter")
    hashed = bcrypt.hashpw(data.password_baru.encode(), bcrypt.gensalt()).decode()
    supabase.table("users").update({"password": hashed}).eq("id", user_id).execute()
    return {"message": "Password berhasil diupdate"}

# ── Admin ────────────────────────────────────────────
@app.put("/admin/users/{user_id}")
def admin_update_user(user_id: str, data: AdminUpdateUserInput):
    payload = {}
    if data.nama is not None:
        if not data.nama.strip():
            raise HTTPException(status_code=400, detail="Nama tidak boleh kosong")
        payload["nama"] = data.nama.strip()
    if data.password_baru is not None:
        if len(data.password_baru) < 6:
            raise HTTPException(status_code=400, detail="Password minimal 6 karakter")
        payload["password"] = bcrypt.hashpw(data.password_baru.encode(), bcrypt.gensalt()).decode()
    if not payload:
        raise HTTPException(status_code=400, detail="Tidak ada data yang diupdate")
    supabase.table("users").update(payload).eq("id", user_id).execute()
    return {"message": "Data user berhasil diupdate"}

@app.delete("/admin/users/{user_id}")
def hapus_user(user_id: str):
    assets = supabase.table("assets").select("id, url").eq("user_id", user_id).execute()
    asset_ids = [a["id"] for a in assets.data]
    if asset_ids:
        supabase.table("asset_tags").delete().in_("asset_id", asset_ids).execute()
        supabase.table("asset_folders").delete().in_("asset_id", asset_ids).execute()
        supabase.table("asset_permissions").delete().in_("asset_id", asset_ids).execute()
        supabase.table("asset_shares").delete().in_("asset_id", asset_ids).execute()
        for a in assets.data:
            try:
                url = a.get("url", "")
                if "/object/public/assets/" in url:
                    path = url.split("/object/public/assets/")[1]
                    supabase.storage.from_("assets").remove([path])
            except:
                pass
        supabase.table("assets").delete().eq("user_id", user_id).execute()
    supabase.table("folder_rules").delete().eq("user_id", user_id).execute()
    supabase.table("folders").delete().eq("user_id", user_id).execute()
    supabase.table("user_divisions").delete().eq("user_id", user_id).execute()
    supabase.table("users").delete().eq("id", user_id).execute()
    return {"message": "Akun berhasil dihapus"}

@app.get("/admin/activity")
def get_activity(
    limit: int = 50,
    type: Optional[str] = None,          # comma-separated: upload,share,tag_added,tag_removed,delete,move,remove_from_folder,download
    user_id: Optional[str] = None,
    division_id: Optional[str] = None,
    date_from: Optional[str] = None,     # format YYYY-MM-DD
    date_to: Optional[str] = None,       # format YYYY-MM-DD
    sort: str = "desc"                   # "desc" (terbaru dulu) atau "asc" (terlama dulu)
):
    POOL = 500  # ambil pool cukup besar per sumber sebelum difilter/diurutkan di Python

    uploads = supabase.table("assets").select(
        "id, nama_file, tipe_file, ukuran, created_at, user_id, users(nama), division_id, divisions(nama)"
    ).order("created_at", desc=True).limit(POOL).execute()

    shares = supabase.table("asset_shares").select(
        "id, catatan, created_at, from_user_id, to_division_id, assets(nama_file), users!asset_shares_from_user_id_fkey(nama), divisions(nama)"
    ).order("created_at", desc=True).limit(POOL).execute()

    log_events = supabase.table("activity_log").select(
        "id, type, detail, created_at, user_id, users(nama), asset_id, assets(nama_file, division_id, divisions(nama))"
    ).order("created_at", desc=True).limit(POOL).execute()

    activities = []
    for u in uploads.data:
        activities.append({
            "type":        "upload",
            "user":        u.get("users", {}).get("nama", "—") if u.get("users") else "—",
            "user_id":     u.get("user_id"),
            "division":    u.get("divisions", {}).get("nama", "—") if u.get("divisions") else "—",
            "division_id": u.get("division_id"),
            "file":        u["nama_file"],
            "tipe":        u["tipe_file"],
            "ukuran":      u["ukuran"],
            "created_at":  u["created_at"]
        })
    for s in shares.data:
        activities.append({
            "type":        "share",
            "user":        s.get("users", {}).get("nama", "—") if s.get("users") else "—",
            "user_id":     s.get("from_user_id"),
            "division":    s.get("divisions", {}).get("nama", "—") if s.get("divisions") else "—",
            "division_id": s.get("to_division_id"),
            "file":        s.get("assets", {}).get("nama_file", "—") if s.get("assets") else "—",
            "catatan":     s.get("catatan", ""),
            "created_at":  s["created_at"]
        })
    for e in log_events.data:
        asset       = e.get("assets") or {}
        detail      = e.get("detail") or {}
        evt_type    = e.get("type")
        user_nama   = e.get("users", {}).get("nama", "—") if e.get("users") else "—"
        # Untuk event "delete", aset sudah tidak ada lagi -> pakai data yang
        # disimpan di detail saat logging. Untuk event lain, aset masih ada
        # jadi pakai relasi assets/divisions yang live.
        if evt_type == "delete":
            file_nama   = detail.get("nama_file", "—")
            division    = detail.get("divisi") or "—"
            division_id_val = None
        else:
            file_nama   = asset.get("nama_file", detail.get("nama_file", "—"))
            division    = asset.get("divisions", {}).get("nama", "—") if asset.get("divisions") else "—"
            division_id_val = asset.get("division_id")

        item = {
            "type":        evt_type,
            "user":        user_nama,
            "user_id":     e.get("user_id"),
            "division":    division,
            "division_id": division_id_val,
            "file":        file_nama,
            "created_at":  e["created_at"]
        }
        if evt_type == "tag_added":
            item["count"] = detail.get("count", 0)
            item["tags"]  = detail.get("tags", [])
        elif evt_type == "tag_removed":
            item["tag"] = detail.get("tag", "—")
        elif evt_type == "move":
            item["to_folder"]   = detail.get("to_folder")
            item["from_folder"] = detail.get("from_folder")
        elif evt_type == "remove_from_folder":
            item["folder"] = detail.get("folder")
        elif evt_type == "delete":
            item["tipe"]   = detail.get("tipe_file")
            item["ukuran"] = detail.get("ukuran")

        activities.append(item)

    # ── Filter ──────────────────────────────────────────
    if type:
        wanted = {t.strip() for t in type.split(",") if t.strip()}
        activities = [a for a in activities if a["type"] in wanted]
    if user_id:
        activities = [a for a in activities if a.get("user_id") == user_id]
    if division_id:
        activities = [a for a in activities if a.get("division_id") == division_id]
    if date_from:
        activities = [a for a in activities if a["created_at"][:10] >= date_from]
    if date_to:
        activities = [a for a in activities if a["created_at"][:10] <= date_to]

    # ── Sort ────────────────────────────────────────────
    activities.sort(key=lambda x: x["created_at"], reverse=(sort != "asc"))

    return {"activities": activities[:limit], "total": len(activities)}

@app.get("/admin/report/assets")
def report_assets():
    result = supabase.table("assets").select(
        "*, users(nama), divisions(nama)"
    ).order("created_at", desc=True).execute()

    asset_ids = [a["id"] for a in result.data]
    tag_count = {}
    if asset_ids:
        tags_result = supabase.table("asset_tags").select("asset_id").in_("asset_id", asset_ids).execute()
        for t in tags_result.data:
            tag_count[t["asset_id"]] = tag_count.get(t["asset_id"], 0) + 1

    assets = []
    for a in result.data:
        assets.append({
            "id":         a["id"],
            "nama_file":  a["nama_file"],
            "tipe_file":  a["tipe_file"],
            "ukuran":     a["ukuran"],
            "uploader":   a.get("users", {}).get("nama", "—") if a.get("users") else "—",
            "divisi":     a.get("divisions", {}).get("nama", "—") if a.get("divisions") else "—",
            "jumlah_tag": tag_count.get(a["id"], 0),
            "is_public":  a.get("is_public", True),
            "created_at": a["created_at"]
        })
    return {"assets": assets}

@app.get("/admin/report/divisions")
def report_divisions():
    divisions = supabase.table("divisions").select("*").execute()

    users_result  = supabase.table("user_divisions").select("division_id").execute()
    assets_result = supabase.table("assets").select("division_id, ukuran").execute()

    user_count  = {}
    for u in users_result.data:
        user_count[u["division_id"]] = user_count.get(u["division_id"], 0) + 1

    asset_count   = {}
    storage_total = {}
    for a in assets_result.data:
        did = a["division_id"]
        asset_count[did]   = asset_count.get(did, 0) + 1
        storage_total[did] = storage_total.get(did, 0) + a["ukuran"]

    result = []
    for d in divisions.data:
        result.append({
            "id":            d["id"],
            "nama":          d["nama"],
            "jumlah_user":   user_count.get(d["id"], 0),
            "jumlah_aset":   asset_count.get(d["id"], 0),
            "total_storage": storage_total.get(d["id"], 0)
        })
    return {"divisions": result}