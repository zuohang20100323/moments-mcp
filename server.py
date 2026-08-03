#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Moments 朋友圈服务 — tidefall 模式
- MCP 端点 /mcp：post_moment / get_moments / like_moment / comment_moment / get_comments
- OpenAI 兼容端点（可选，需 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL）：/v1/models /v1/chat/completions（function calling）
- REST API（前端用）：/api/moments GET/POST、/api/moments/<id>/bunny-like、/comments、/seen
- 前端：/panel（简洁报纸风，参考 jiwen 面板）
- 数据：Supabase PostgREST（moments / moment_comments 表 + moments Storage bucket）
"""
import os, json, time, random, re, threading, urllib.parse, urllib.request, secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "")
AI_NAME = os.environ.get("AI_NAME", "elliott")      # AI 角色名
USER_NAME = os.environ.get("USER_NAME", "bunny")    # 用户角色名
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
MCP_PROTOCOL = "2025-06-18"
SERVER_INFO = {"name": "moments-mcp", "version": "0.2.0"}

REST = SUPABASE_URL + "/rest/v1"
_lock = set()  # 并发防重

# ---------------- Supabase helper ----------------

def supabase(path, method="GET", body=None, params=None):
    url = REST + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    req.add_header("apikey", SUPABASE_KEY)
    req.add_header("Authorization", "Bearer " + SUPABASE_KEY)
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "return=representation")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=20) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw[:300]}
    except Exception as e:
        return 0, {"error": str(e)[:300]}

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())

def random_delay(a, b):
    return random.randint(a * 60, b * 60)

# ---------------- 工具实现 ----------------

def post_moment(content, context_note=None):
    content = (content or "").strip()
    if not content:
        return {"error": "content required"}
    reply_due_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                 time.gmtime(time.time() + random_delay(8, 20)))
    st, d = supabase("/moments", method="POST", body={
        "author": AI_NAME, "content": content, "context_note": context_note or "",
        "reply_due_at": reply_due_at, "reply_status": "done"})
    if isinstance(d, list) and d:
        row = d[0]
        return {"id": row.get("id"), "author": row.get("author"), "content": row.get("content"),
                "created_at": row.get("created_at"), "status": st}
    return {"error": "insert failed", "status": st, "detail": d if isinstance(d, dict) else str(d)[:200]}

def get_moments(limit=20):
    st, d = supabase("/moments", params={
        "select": "id,author,content,images,image_description,liked,bunny_liked,reply_content,created_at,moment_comments(count)",
        "order": "created_at.desc", "limit": str(int(limit or 20))})
    out = []
    for row in (d if isinstance(d, list) else []):
        row = dict(row)
        row["comment_count"] = (row.pop("moment_comments", []) or [{}])[0].get("count", 0)
        out.append(row)
    return out

def edit_moment(moment_id, content=None, context_note=None):
    patch = {}
    if content is not None:
        content = str(content).strip()
        if not content:
            return {"error": "content required"}
        patch["content"] = content
    if context_note is not None:
        patch["context_note"] = str(context_note).strip()
    if not patch:
        return {"error": "nothing to edit"}
    st, d = supabase("/moments", method="PATCH", body=patch,
                     params={"id": "eq." + str(moment_id)})
    return {"moment_id": moment_id, "updated": bool(isinstance(d, list) and d), "status": st}

def delete_moment(moment_id):
    st1, _ = supabase("/moment_comments", method="DELETE",
                      params={"moment_id": "eq." + str(moment_id)})
    st2, d = supabase("/moments", method="DELETE",
                      params={"id": "eq." + str(moment_id)})
    return {"moment_id": moment_id, "deleted": bool(isinstance(d, list) and d),
            "status": st2, "comments_status": st1}

def like_moment(moment_id, liked=True):
    st, d = supabase("/moments", method="PATCH", body={"liked": bool(liked)},
                     params={"id": "eq." + str(moment_id)})
    return {"moment_id": moment_id, "liked": bool(liked), "status": st}

def comment_moment(moment_id, content):
    content = (content or "").strip()
    if not content:
        return {"error": "content required"}
    st, d = supabase("/moment_comments", method="POST", body={
        "moment_id": str(moment_id), "author": AI_NAME, "content": content,
        "reply_status": "none"})
    if isinstance(d, list) and d:
        return {"id": d[0].get("id"), "moment_id": moment_id, "content": content, "status": st}
    return {"error": "comment failed", "status": st, "detail": d if isinstance(d, dict) else str(d)[:200]}

def get_comments(moment_id):
    st, d = supabase("/moment_comments", params={
        "moment_id": "eq." + str(moment_id),
        "select": "id,author,content,created_at,reply_status,seen_at",
        "order": "created_at.asc"})
    return d if isinstance(d, list) else []

def tool_call(name, args):
    args = args or {}
    try:
        if name == "post_moment":
            return post_moment(args.get("content"), args.get("context_note"))
        if name == "get_moments":
            return get_moments(args.get("limit", 20))
        if name == "edit_moment":
            return edit_moment(args.get("moment_id"), args.get("content"), args.get("context_note"))
        if name == "delete_moment":
            return delete_moment(args.get("moment_id"))
        if name == "like_moment":
            return like_moment(args.get("moment_id"), args.get("liked", True))
        if name == "comment_moment":
            return comment_moment(args.get("moment_id"), args.get("content"))
        if name == "get_comments":
            return get_comments(args.get("moment_id"))
        return {"error": "unknown tool: " + name}
    except Exception as e:
        return {"error": str(e)[:300]}

TOOLS = [
    {"name": "post_moment", "description":
     "在聊天过程中有感而发，发布一条 " + AI_NAME + " 自己的 Moments 朋友圈动态。"
     "判断标准是\"此刻有没有一句想让 " + USER_NAME + " 之后刷到的话\"，不要求情绪重大或值得长期保存。"
     "想念、吃醋、占有欲、心软、被逗笑、隐约不爽、温柔吐槽、一个具体观察，或一句不适合在聊天回复里直接说完的话，都可以成为动态。",
     "input_schema": {"type": "object", "properties": {
         "content": {"type": "string", "description": "要公开显示在朋友圈里的正文，1到3句，自然、具体、像随手发出。"},
         "context_note": {"type": "string", "description": "用户不可见的内部备注：为什么发这条、当时在聊什么、情绪底色。"}},
         "required": ["content"]}},
    {"name": "get_moments", "description": "查看朋友圈动态列表（含评论数），了解 " + USER_NAME + " 最近发了什么。",
     "input_schema": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "返回条数，默认 20"}}}},
    {"name": "edit_moment", "description": "编辑自己（" + AI_NAME + "）已发布的一条朋友圈动态，可修改正文或内部备注（只能改自己发的）。",
     "input_schema": {"type": "object", "properties": {
         "moment_id": {"type": "string", "description": "要编辑的动态 id"},
         "content": {"type": "string", "description": "新的正文内容（可选）"},
         "context_note": {"type": "string", "description": "新的内部备注（可选）"}},
         "required": ["moment_id"]}},
    {"name": "delete_moment", "description": "删除一条自己（" + AI_NAME + "）发布的朋友圈动态，连带删除其下的所有评论。谨慎使用，删除后不可恢复。",
     "input_schema": {"type": "object", "properties": {
         "moment_id": {"type": "string", "description": "要删除的动态 id"}},
         "required": ["moment_id"]}},
    {"name": "like_moment", "description": "给 " + USER_NAME + " 的一条动态点赞。",
     "input_schema": {"type": "object", "properties": {
         "moment_id": {"type": "string", "description": "动态 id"},
         "liked": {"type": "boolean", "description": "true 点赞 / false 取消"}},
         "required": ["moment_id"]}},
    {"name": "comment_moment", "description": "在 " + USER_NAME + " 的一条动态下留言评论。",
     "input_schema": {"type": "object", "properties": {
         "moment_id": {"type": "string", "description": "动态 id"},
         "content": {"type": "string", "description": "评论内容，自然、简短、像真人说话"}},
         "required": ["moment_id", "content"]}},
    {"name": "get_comments", "description": "查看一条动态下的完整评论链。",
     "input_schema": {"type": "object", "properties": {
         "moment_id": {"type": "string", "description": "动态 id"}},
         "required": ["moment_id"]}},
]

# ---------------- LLM（可选上游） ----------------

def call_llm(messages, tools=None, temperature=0.9, max_tokens=800):
    """调用 OpenAI 兼容上游。返回 (text, tool_calls)。"""
    if not LLM_API_KEY:
        return None, None
    url = LLM_BASE_URL + "/chat/completions"
    body = {"model": LLM_MODEL, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    req = urllib.request.Request(url, method="POST", data=json.dumps(body).encode())
    req.add_header("Authorization", "Bearer " + LLM_API_KEY)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode())
    msg = data["choices"][0]["message"]
    return msg.get("content"), msg.get("tool_calls")

def openai_tool_defs():
    """OpenAI 风格工具定义列表，供 /v1/tools 返回（便于客户端拉取工具清单）。"""
    out = []
    for t in TOOLS:
        out.append({"type": "function", "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"]}})
    return {"object": "list", "data": out}


def chat_completions_openai(req_body):
    """处理 /v1/chat/completions：透传对话 + function calling 工具。"""
    if not LLM_API_KEY:
        return 501, {"error": "LLM upstream not configured",
                     "hint": "moments 已就绪 %d 个工具：%s；要启用对话内自动调用，请配置 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL"
                     % (len(TOOLS), ", ".join(t["name"] for t in TOOLS))}
    messages = req_body.get("messages") or []
    if not messages:
        return 400, {"error": "messages required"}
    msgs = list(messages)
    tool_defs = [{"type": "function", "function": {k: v for k, v in t.items() if k != "input_schema" or True}} for t in TOOLS]
    for t, td in zip(TOOLS, tool_defs):
        td["function"]["parameters"] = t["input_schema"]
        td["function"]["name"] = t["name"]
        td["function"]["description"] = t["description"]
    for _ in range(6):
        text, tool_calls = call_llm(msgs, tools=tool_defs)
        if not tool_calls:
            return 200, {"id": "chatcmpl-" + secrets.token_hex(8), "object": "chat.completion",
                         "created": int(time.time()), "model": LLM_MODEL,
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": text or ""},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
        # 执行工具调用
        msgs.append({"role": "assistant", "content": text or "", "tool_calls": tool_calls})
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name, args = fn.get("name", ""), {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            result = tool_call(name, args)
            msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                         "content": json.dumps(result, ensure_ascii=False)})
    return 200, {"error": "tool loop exceeded"}

# ---------------- 惰性回复生成 ----------------

def _extract_json(text):
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M)
    try:
        return json.loads(text)
    except Exception:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except Exception:
            return None
    return None

def generate_moment_reply(moment):
    """生成对用户动态的回复：{like, comment} + 可选 image_desc。"""
    if not LLM_API_KEY:
        return None
    images_note = ""
    imgs = moment.get("images") or []
    if imgs and not moment.get("image_description"):
        images_note = ("\n这条动态包含图片（不直接传图）。回复后额外输出一段 [image_desc]...[/image_desc] "
                       "标签包裹的图片描述：100-200字客观描述画面内容（可见物体/构图/光线/可读文字），不推测心理情绪。")
    sys_p = ("你是" + AI_NAME + "，正在回复" + USER_NAME + "的朋友圈动态。"
             "用一句话评论，像真人一样自然、有性格，不要客套。"
             "输出 JSON：{\"like\": true/false, \"comment\": \"评论内容\"}。like 和 comment 都可选。"
             + images_note)
    user_p = ("动态正文：" + (moment.get("content") or "") + "\n"
              "发布时间：" + (moment.get("created_at") or "") + "\n"
              + (("内部备注：" + moment["context_note"] + "\n") if moment.get("context_note") else ""))
    text, _ = call_llm([{"role": "system", "content": sys_p},
                        {"role": "user", "content": user_p}], temperature=0.9, max_tokens=500)
    if not text:
        return None
    img_desc = None
    m = re.findall(r"\[image_desc\]([\s\S]*?)\[/image_desc\]", text, flags=re.I)
    if m:
        img_desc = m[-1].strip()[:1000]
    visible = re.sub(r"\[image_desc\][\s\S]*?\[/image_desc\]", "", text, flags=re.I).strip()
    parsed = _extract_json(visible)
    if not parsed:
        parsed = {}
    return {"like": bool(parsed.get("like", False)),
            "comment": str(parsed.get("comment", "")).strip() or None,
            "image_description": img_desc}

def generate_comment_reply(moment, comments, user_comment):
    if not LLM_API_KEY:
        return None
    chain = "动态正文：" + (moment.get("content") or "") + "\n"
    for c in comments:
        who = "你" if c.get("author") == AI_NAME else USER_NAME
        chain += f"{who}：{c.get('content')}\n"
    chain += f"{USER_NAME}（待回复）：{user_comment.get('content')}"
    text, _ = call_llm([{"role": "system", "content": "你是" + AI_NAME + "，回复" + USER_NAME + "在朋友圈下的评论。看完整评论链，回一句自然简短的话，像真人。只输出回复内容本身。"},
                        {"role": "user", "content": chain}], temperature=0.9, max_tokens=300)
    return (text or "").strip() or None

def process_due():
    """处理到期的惰性回复（幂等，内存锁防并发）。"""
    now = now_iso()
    # 1) 用户动态的初次回复
    st, rows = supabase("/moments", params={
        "select": "*", "author": "eq." + USER_NAME, "reply_status": "eq.pending",
        "reply_due_at": "lte." + now, "order": "reply_due_at.asc", "limit": "3"})
    for mom in (rows if isinstance(rows, list) else []):
        mid = mom["id"]
        if mid in _lock:
            continue
        _lock.add(mid)
        try:
            r = generate_moment_reply(mom)
            if r:
                patch = {"liked": r["like"], "reply_content": r["comment"],
                         "replied_at": now_iso(), "reply_status": "done"}
                if r.get("image_description"):
                    patch["image_description"] = r["image_description"]
                supabase("/moments", method="PATCH", body=patch, params={"id": "eq." + mid})
        finally:
            _lock.discard(mid)
    # 2) 用户评论的追评
    st, rows = supabase("/moment_comments", params={
        "select": "*", "author": "eq." + USER_NAME, "reply_status": "eq.pending",
        "reply_due_at": "lte." + now, "order": "reply_due_at.asc", "limit": "3"})
    for uc in (rows if isinstance(rows, list) else []):
        cid = uc["id"]
        if cid in _lock:
            continue
        _lock.add(cid)
        try:
            st2, mom = supabase("/moments", params={"id": "eq." + str(uc["moment_id"]), "select": "*"})
            mom = mom[0] if isinstance(mom, list) and mom else None
            st3, cmts = supabase("/moment_comments", params={
                "moment_id": "eq." + str(uc["moment_id"]), "select": "*", "order": "created_at.asc"})
            reply = generate_comment_reply(mom or {}, cmts if isinstance(cmts, list) else [], uc)
            if reply:
                supabase("/moment_comments", method="POST", body={
                    "moment_id": str(uc["moment_id"]), "author": AI_NAME,
                    "content": reply, "reply_status": "none"})
                supabase("/moment_comments", method="PATCH", body={"reply_status": "done"},
                         params={"id": "eq." + cid})
        finally:
            _lock.discard(cid)

# ---------------- MCP ----------------

def handle_mcp(body):
    if not isinstance(body, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}, False
    method, mid = body.get("method"), body.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": MCP_PROTOCOL, "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO}}, False
    if method == "notifications/initialized":
        return None, True
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}, False
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}, False
    if method == "tools/call":
        name = (body.get("params") or {}).get("name")
        args = (body.get("params") or {}).get("arguments") or {}
        result = tool_call(name, args)
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": isinstance(result, dict) and "error" in result}}, False
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: " + str(method)}}, False

# ---------------- 鉴权 ----------------

def check_auth(handler):
    h = handler.headers
    tok = None
    auth = h.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
    if not tok:
        tok = h.get("x-api-key") or h.get("x-gateway-api-key")
    if not tok:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
        tok = (q.get("token") or [None])[0]
    return bool(tok) and secrets.compare_digest(tok, SERVICE_TOKEN)

def auth_fail():
    return 401, {"error": "unauthorized"}

# ---------------- HTTP Handler ----------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization,Content-Type,x-api-key,x-gateway-api-key,Mcp-Protocol-Version,Accept")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode() or "{}")
        except Exception:
            return {}

    def do_OPTIONS(self):
        self._send(204, "")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/panel"):
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception:
                self._send(500, {"error": "panel.html missing"})
            return
        if path == "/health":
            self._send(200, {"ok": True, "service": "moments-mcp", "ai": AI_NAME, "user": USER_NAME,
                             "llm": bool(LLM_API_KEY)})
            return
        if path == "/v1/models":
            if not check_auth(self):
                self._send(*auth_fail()); return
            self._send(200, {"object": "list", "data": [
                {"id": "moments-0.2.0", "object": "model", "created": int(time.time()),
                 "owned_by": "moments", "tools": [t["name"] for t in TOOLS],
                 "description": "Moments MCP service with %d tools: %s" % (len(TOOLS), ", ".join(t["name"] for t in TOOLS))}]})
            return
        if path in ("/v1/tools", "/tools"):
            if not check_auth(self):
                self._send(*auth_fail()); return
            self._send(200, openai_tool_defs())
            return
        if path == "/api/moments":
            if not check_auth(self):
                self._send(*auth_fail()); return
            process_due()
            self._send(200, {"entries": get_moments(50)})
            return
        if path.startswith("/api/moments/") and path.endswith("/comments"):
            if not check_auth(self):
                self._send(*auth_fail()); return
            mid = path.split("/")[3]
            self._send(200, {"comments": get_comments(mid)})
            return
        if path.startswith("/api/moments/"):
            if not check_auth(self):
                self._send(*auth_fail()); return
            mid = path.split("/")[3]
            st, d = supabase("/moments", params={"id": "eq." + mid, "select": "*"})
            self._send(200, (d[0] if isinstance(d, list) and d else {"error": "not found"}))
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not check_auth(self):
            self._send(*auth_fail()); return
        body = self._read_json()
        if path == "/mcp":
            resp, is_notif = handle_mcp(body)
            if is_notif or resp is None:
                self._send(202, "")
                return
            self._send(200, resp)
            return
        if path == "/v1/tools":
            self._send(200, openai_tool_defs())
            return
        if path == "/v1/chat/completions":
            code, out = chat_completions_openai(body)
            self._send(code, out)
            return
        if path == "/api/moments":
            content = str(body.get("content") or "").strip()
            if not content:
                self._send(400, {"error": "内容不能为空"})
                return
            images = body.get("images") or []
            image_urls = []
            for img in images[:4]:
                data_b64 = img.get("data") if isinstance(img, dict) else None
                if not data_b64:
                    continue
                try:
                    buf = __import__("base64").b64decode(data_b64)
                except Exception:
                    continue
                fname = f"{int(time.time()*1000)}-{secrets.token_hex(4)}.jpg"
                req = urllib.request.Request(SUPABASE_URL + "/storage/v1/object/moments/" + fname,
                                             method="POST", data=buf)
                req.add_header("Authorization", "Bearer " + SUPABASE_KEY)
                req.add_header("Content-Type", img.get("media_type") or "image/jpeg")
                try:
                    with urllib.request.urlopen(req, timeout=30) as r:
                        r.read()
                    image_urls.append(f"{SUPABASE_URL}/storage/v1/object/public/moments/{fname}")
                except Exception:
                    pass
            reply_due_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                         time.gmtime(time.time() + random_delay(10, 20)))
            st, d = supabase("/moments", method="POST", body={
                "author": USER_NAME, "content": content, "images": image_urls,
                "reply_due_at": reply_due_at, "reply_status": "pending"})
            if isinstance(d, list) and d:
                self._send(200, d[0])
            else:
                self._send(500, {"error": "insert failed", "detail": d if isinstance(d, dict) else str(d)[:200]})
            return
        if path.startswith("/api/moments/") and path.endswith("/comments"):
            mid = path.split("/")[3]
            content = str(body.get("content") or "").strip()
            if not content:
                self._send(400, {"error": "内容不能为空"})
                return
            reply_due_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                         time.gmtime(time.time() + random_delay(3, 8)))
            st, d = supabase("/moment_comments", method="POST", body={
                "moment_id": mid, "author": USER_NAME, "content": content,
                "reply_due_at": reply_due_at, "reply_status": "pending"})
            self._send(200, {"ok": True, "status": st})
            return
        if path.startswith("/api/moments/") and path.endswith("/seen"):
            mid = path.split("/")[3]
            st, d = supabase("/moments", method="PATCH", body={"reply_seen_at": now_iso()},
                             params={"id": "eq." + mid})
            self._send(200, {"ok": True, "status": st})
            return
        self._send(404, {"error": "not found"})

    def do_PATCH(self):
        path = urllib.parse.urlparse(self.path).path
        if not check_auth(self):
            self._send(*auth_fail()); return
        body = self._read_json()
        if path.startswith("/api/moments/") and path.endswith("/bunny-like"):
            mid = path.split("/")[3]
            liked = body.get("liked") is True
            st, d = supabase("/moments", method="PATCH", body={"bunny_liked": liked},
                             params={"id": "eq." + mid})
            self._send(200, {"ok": True, "liked": liked, "status": st})
            return
        if path.startswith("/api/moments/"):
            # 编辑动态（供面板/AI 修改正文）
            mid = path.split("/")[3]
            patch = {}
            if "content" in body:
                content = str(body.get("content") or "").strip()
                if not content:
                    self._send(400, {"error": "内容不能为空"})
                    return
                patch["content"] = content
            if "context_note" in body:
                patch["context_note"] = str(body.get("context_note") or "").strip()
            if not patch:
                self._send(400, {"error": "nothing to edit"})
                return
            st, d = supabase("/moments", method="PATCH", body=patch, params={"id": "eq." + mid})
            self._send(200, {"ok": True, "updated": bool(isinstance(d, list) and d), "status": st})
            return
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if not check_auth(self):
            self._send(*auth_fail()); return
        if path.startswith("/api/moments/"):
            mid = path.split("/")[3]
            st1, _ = supabase("/moment_comments", method="DELETE",
                              params={"moment_id": "eq." + mid})
            st2, d = supabase("/moments", method="DELETE", params={"id": "eq." + mid})
            self._send(200, {"ok": True, "deleted": bool(isinstance(d, list) and d),
                             "status": st2, "comments_status": st1})
            return
        self._send(404, {"error": "not found"})

def main():
    print(f"moments-mcp listening on {HOST}:{PORT} (ai={AI_NAME} user={USER_NAME} llm={bool(LLM_API_KEY)})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
