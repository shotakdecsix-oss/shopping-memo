"""
買い物メモアプリ - Flask バックエンド
夫婦間でURL共有し、店タイプ・売り場カテゴリ別に整理された買い物リストを管理する。
Config: shopping_config.json (DO NOT write API keys in chat or code comments)
"""

import json
import os
import threading
import uuid
import anthropic
from flask import Flask, request, jsonify, send_from_directory, redirect
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "shopping_config.json")
DATA_PATH = os.path.join(BASE_DIR, "lists_data.json")

CFG = {}
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        CFG = json.load(f)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY") or CFG.get("anthropic_api_key", "")
MODEL         = os.environ.get("MODEL")              or CFG.get("model", "claude-haiku-4-5")
PORT          = int(os.environ.get("PORT", CFG.get("port", 5053)))

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
app = Flask(__name__, static_folder=BASE_DIR)

JST = timezone(timedelta(hours=9))
SERVER_START = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

# ---------------------------------------------------------------------------
# 店タイプ・売り場カテゴリ プリセット
# ---------------------------------------------------------------------------
STORE_CATEGORIES = {
    "スーパー":      ["野菜・果物", "精肉", "魚介", "乳製品・卵", "冷凍食品", "調味料", "パン・米", "飲料", "惣菜", "その他"],
    "ドラッグストア": ["医薬品", "日用品", "コスメ・スキンケア", "ベビー用品", "その他"],
    "百均":         ["その他"],
    "モール系":      ["服", "靴", "雑貨", "その他"],
}
STORE_TYPES = list(STORE_CATEGORIES.keys())

# ---------------------------------------------------------------------------
# データ永続化（リストIDごとに items / frequent を保持。ファイルベース）
# ---------------------------------------------------------------------------
_data_lock = threading.Lock()

def _load_data() -> dict:
    if not os.path.exists(DATA_PATH):
        return {}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_data(data: dict) -> None:
    tmp_path = DATA_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, DATA_PATH)

_DATA = _load_data()

def _get_list(list_id: str) -> dict:
    with _data_lock:
        if list_id not in _DATA:
            _DATA[list_id] = {"items": [], "frequent": []}
            _save_data(_DATA)
        return _DATA[list_id]

# ---------------------------------------------------------------------------
# Claude によるカテゴリ分類
# ---------------------------------------------------------------------------
def classify_item(name: str) -> dict:
    """商品名から店タイプ・売り場カテゴリをAIで推定。失敗時は「スーパー/その他」にフォールバック。"""
    categories_desc = "\n".join(
        f"- {store}: {', '.join(cats)}" for store, cats in STORE_CATEGORIES.items()
    )
    prompt = f"""以下の商品名から、最も適切な「店タイプ」と「売り場カテゴリ」を1つずつ選んでください。

【商品名】
{name}

【選択肢】
{categories_desc}

必ず選択肢の中からstore・categoryをそれぞれ1つずつ選び、次のJSON形式のみで出力してください（説明文不要）:
{{"store": "店タイプ", "category": "売り場カテゴリ"}}"""

    fallback = {"store": "スーパー", "category": "その他"}
    if not ANTHROPIC_KEY:
        return fallback
    try:
        message = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end + 1]
        result = json.loads(raw)
        store = result.get("store", "")
        category = result.get("category", "")
        if store not in STORE_CATEGORIES:
            store = fallback["store"]
        if category not in STORE_CATEGORIES[store]:
            category = STORE_CATEGORIES[store][-1]  # 「その他」相当
        return {"store": store, "category": category}
    except Exception as e:
        print(f"[WARN] classify_item failed: {e}")
        return fallback


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def root():
    new_id = uuid.uuid4().hex[:8]
    _get_list(new_id)
    return redirect(f"/l/{new_id}")


@app.route("/l/<list_id>")
def list_page(list_id):
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/version")
def version():
    return jsonify({"deployed_at": SERVER_START})


@app.route("/api/meta")
def meta():
    return jsonify({"store_categories": STORE_CATEGORIES})


@app.route("/api/list/<list_id>")
def get_list(list_id):
    lst = _get_list(list_id)
    return jsonify(lst)


@app.route("/api/list/<list_id>/items", methods=["POST"])
def add_item(list_id):
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    nickname = (body.get("nickname") or "").strip()
    if not name:
        return jsonify({"error": "商品名を入力してください"}), 400

    lst = _get_list(list_id)

    # よく買うものリストに登録済みならカテゴリを再利用（API節約）
    freq_match = next((f for f in lst["frequent"] if f["name"] == name), None)
    if freq_match:
        cls = {"store": freq_match["store"], "category": freq_match["category"]}
    else:
        cls = classify_item(name)

    item = {
        "id": uuid.uuid4().hex[:10],
        "name": name,
        "store": cls["store"],
        "category": cls["category"],
        "added_by": nickname,
        "added_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
    }
    with _data_lock:
        lst["items"].append(item)
        _save_data(_DATA)
    return jsonify(lst)


@app.route("/api/list/<list_id>/items/<item_id>/category", methods=["POST"])
def update_category(list_id, item_id):
    body = request.get_json(force=True)
    store = body.get("store")
    category = body.get("category")
    if store not in STORE_CATEGORIES or category not in STORE_CATEGORIES[store]:
        return jsonify({"error": "不正なカテゴリです"}), 400

    lst = _get_list(list_id)
    with _data_lock:
        for it in lst["items"]:
            if it["id"] == item_id:
                it["store"] = store
                it["category"] = category
                break
        _save_data(_DATA)
    return jsonify(lst)


@app.route("/api/list/<list_id>/items/<item_id>/check", methods=["POST"])
def check_item(list_id, item_id):
    lst = _get_list(list_id)
    with _data_lock:
        target = next((it for it in lst["items"] if it["id"] == item_id), None)
        if target:
            lst["items"] = [it for it in lst["items"] if it["id"] != item_id]
            # よく買うものリストへ登録（重複は上書き更新のみ）
            lst["frequent"] = [f for f in lst["frequent"] if f["name"] != target["name"]]
            lst["frequent"].insert(0, {
                "name": target["name"], "store": target["store"], "category": target["category"],
            })
            lst["frequent"] = lst["frequent"][:30]  # 上限30件
        _save_data(_DATA)
    return jsonify(lst)


@app.route("/api/list/<list_id>/frequent/add", methods=["POST"])
def add_from_frequent(list_id):
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    nickname = (body.get("nickname") or "").strip()
    lst = _get_list(list_id)
    freq_match = next((f for f in lst["frequent"] if f["name"] == name), None)
    if not freq_match:
        return jsonify({"error": "よく買うものリストに見つかりません"}), 404

    item = {
        "id": uuid.uuid4().hex[:10],
        "name": freq_match["name"],
        "store": freq_match["store"],
        "category": freq_match["category"],
        "added_by": nickname,
        "added_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
    }
    with _data_lock:
        lst["items"].append(item)
        _save_data(_DATA)
    return jsonify(lst)


@app.route("/api/list/<list_id>/frequent/<name>", methods=["DELETE"])
def remove_frequent(list_id, name):
    lst = _get_list(list_id)
    with _data_lock:
        lst["frequent"] = [f for f in lst["frequent"] if f["name"] != name]
        _save_data(_DATA)
    return jsonify(lst)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"[Shopping Memo] Starting on http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
