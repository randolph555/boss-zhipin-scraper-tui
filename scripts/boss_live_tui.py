#!/usr/bin/env python3
"""Realtime terminal UI for BOSS Zhipin jobs via Chrome CDP."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import webbrowser
from urllib.parse import urlencode, urlparse

try:
    from rich.markup import escape
except ImportError:
    def escape(value):
        return str(value)

try:
    from scripts import boss_cdp_raw as boss
except ImportError:
    import boss_cdp_raw as boss

try:
    from textual import work
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import DataTable, Footer, Header, Input, Label, LoadingIndicator, Static
except ImportError:  # pragma: no cover - exercised by users without optional dependency
    work = None
    App = object
    ComposeResult = object
    Horizontal = Vertical = VerticalScroll = DataTable = Footer = Header = Input = Label = LoadingIndicator = Static = None


PAGE_SIZE = 30
DETAIL_WAIT_SECONDS = 4
DETAIL_CACHE_TTL_SECONDS = 30 * 60
PAGE_SQLITE_CACHE_TTL_SECONDS = 30 * 60
DETAIL_SQLITE_CACHE_TTL_SECONDS = 24 * 60 * 60
CHAT_READY_WAIT_SECONDS = 4.0
CHAT_POLL_INTERVAL_SECONDS = 0.2
CONVERSATION_OPEN_WAIT_SECONDS = 2.5
CONVERSATION_OPEN_DEBOUNCE_SECONDS = 0.25
DETAIL_AUTOLOAD_DELAY_SECONDS = 0.8
DETAIL_MIN_INTERVAL_SECONDS = 4.0
DETAIL_RISK_PAUSE_SECONDS = 10 * 60
DEFAULT_SQLITE_CACHE_PATH = os.path.join(os.getcwd(), ".cache", "boss_live_cache.sqlite3")

COLOR_BG = "#0b1016"
COLOR_PANEL = "#0f171f"
COLOR_PANEL_ALT = "#121b24"
COLOR_BORDER = "#263241"
COLOR_BORDER_SOFT = "#334255"
COLOR_TEXT = "#dbe3ee"
COLOR_TEXT_STRONG = "#f4f7fb"
COLOR_MUTED = "#8ea0b4"
COLOR_FAINT = "#647184"
COLOR_ACCENT = "#f2c86b"
COLOR_BLUE = "#82aaff"
COLOR_GREEN = "#73daca"
COLOR_SUCCESS = "#9ece6a"
COLOR_WARNING = "#f2c86b"
COLOR_ME_BODY = "#b8f4e2"
COLOR_BOSS_BODY = "#dbe3ee"
COLOR_HINT = "#9ab0c9"
COLOR_HINT_DIM = "#708296"


def require_textual():
    if work is not None:
        return True
    print("缺少依赖: textual")
    print("请安装（任选其一）:")
    print("  uv add textual")
    print("  pip install textual")
    return False


def short_text(value, limit):
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def first_nonempty(*values):
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return "-"


def format_filter_summary(filters):
    if not filters:
        return "筛选: 全部"
    return "筛选: " + " · ".join(f"{key}={value}" for key, value in filters.items())


def build_filters(args):
    filters = {}
    for key in ["scale", "stage", "salary", "experience", "degree", "industry"]:
        value = getattr(args, key)
        if value:
            filters[key] = value
    return filters


def build_agent_context(keyword, city_name, filters, job, detail=None):
    """Return the stable payload future MCP/agent/chat integrations should consume.

    Keep this JSON-serializable and UI-agnostic. The next stage can expose it as
    an MCP resource/tool or feed it into an in-TUI AI chat panel without coupling
    agents to Textual widgets or CDP internals.
    """
    detail = detail or {}
    return {
        "source": {
            "mode": "live_cdp",
            "keyword": keyword,
            "city": city_name,
            "filters": filters or {},
            "page": job.get("_live_page"),
        },
        "job": {
            "job_id": job.get("job_id", ""),
            "title": job.get("title", ""),
            "salary": job.get("salary", ""),
            "salary_source": job.get("salary_source", ""),
            "company": job.get("boss_name", ""),
            "location": job.get("location", ""),
            "requirements": job.get("tags", ""),
            "skills": job.get("skills", ""),
            "labels": job.get("job_labels", ""),
            "welfare": job.get("welfare", ""),
            "company_scale": job.get("company_scale", ""),
            "company_stage": job.get("company_stage", ""),
            "company_industry": job.get("company_industry", ""),
            "job_link": job.get("job_link", ""),
            "company_link": job.get("company_link", ""),
        },
        "detail": {
            "loaded": bool(detail),
            "skill_tags": detail.get("skill_tags", []),
            "jd": detail.get("jd", ""),
        },
    }


def looks_like_risk_page(text):
    value = str(text or "")
    risk_markers = (
        "验证码",
        "安全验证",
        "访问异常",
        "环境异常",
        "请完成验证",
        "拖动滑块",
        "行为异常",
        "captcha",
        "verify",
    )
    return any(marker.lower() in value.lower() for marker in risk_markers)


JD_NOISE_PREFIXES = (
    "微信扫码分享",
    "微 信 扫码分享",
    "举报",
    "举 报",
    "BOSS直聘",
    "来自BOSS直聘",
    "竞争力分析",
    "查看完整个人",
    "安全提示",
    "工商信息",
    "公司介绍",
    "工作地址",
    "看过该职位的人还看了",
    "更多职位",
    "精选职位",
)

JD_STOP_MARKERS = (
    "招聘者",
    "刚刚活跃",
    "今日活跃",
    "招聘专员",
    "招聘主管",
    "人事专员",
    "个人综合排名",
    "你在? 位置",
    "你在？位置",
    "BOSS 安全提示",
    "BOSS安全提示",
    "安全提示",
    "公司介绍",
    "工商信息",
    "工作地址",
    "看过该职位的人还看了",
    "更多职位",
    "精选职位",
)

JD_START_MARKERS = ("职位描述", "职位详情", "岗位职责", "工作职责", "岗位描述")
RECRUITER_STATUS_LINES = ("在线", "刚刚活跃", "今日活跃", "本周活跃", "本月活跃")
RECRUITER_ROLE_LINES = ("招聘专员", "招聘主管", "人事专员", "HR", "HR经理")


def is_recruiter_status(line):
    text = str(line or "").strip()
    return text in RECRUITER_STATUS_LINES or bool(re.fullmatch(r"\d+日内活跃", text))


def is_recruiter_role(line):
    text = str(line or "").strip()
    return text in RECRUITER_ROLE_LINES or bool(re.fullmatch(r"(HR|人事|招聘).{0,8}", text))


def detail_source_lines(jd):
    text = str(jd or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    start_positions = [text.find(marker) for marker in JD_START_MARKERS if text.find(marker) >= 0]
    if start_positions:
        text = text[min(start_positions):]
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line.strip())
        if not line:
            continue
        if any(line.startswith(prefix) for prefix in JD_NOISE_PREFIXES):
            continue
        if line in {"职位描述", "职位详情", "查看全部", "搜索", "一般", "良好", "优秀", "极好"}:
            continue
        lines.append(line)
    return lines


def looks_like_recruiter_name(line):
    return bool(re.fullmatch(r"[\u4e00-\u9fa5]{1,4}(先生|女士|经理|总|主管|HR)", line)) or bool(
        re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", line)
    )


def format_recruiter_info(jd):
    lines = detail_source_lines(jd)
    for index, line in enumerate(lines):
        if not is_recruiter_status(line) and not is_recruiter_role(line) and line != "招聘者":
            continue

        parts = []
        if index > 0 and looks_like_recruiter_name(lines[index - 1]):
            parts.append(lines[index - 1])
        if line != "招聘者":
            parts.append(line)
        for extra in lines[index + 1:index + 5]:
            if any(marker in extra for marker in ("个人综合排名", "BOSS 安全提示", "公司介绍", "工商信息", "工作地址")):
                break
            if extra in {".", "·", "。"}:
                continue
            if len(extra) <= 30 or is_recruiter_role(extra):
                parts.append(extra)

        deduped = []
        seen = set()
        for part in parts:
            if part and part not in seen:
                deduped.append(part)
                seen.add(part)
        return " · ".join(deduped)
    return ""


def remove_trailing_recruiter_lines(lines, aggressive=False):
    cleaned = list(lines)
    while cleaned:
        line = cleaned[-1].strip()
        if re.fullmatch(r"[\u4e00-\u9fa5]{1,4}(先生|女士|经理|总|主管|HR)", line):
            cleaned.pop()
            continue
        if is_recruiter_status(line) or is_recruiter_role(line):
            cleaned.pop()
            continue
        if aggressive and re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", line):
            cleaned.pop()
            continue
        if line in {".", "·", "。"}:
            cleaned.pop()
            continue
        break
    return cleaned


def clean_detail_jd(jd):
    """Lightly clean detail-page text for terminal reading without mutating raw data."""
    text = str(jd or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    start_positions = [text.find(marker) for marker in JD_START_MARKERS if text.find(marker) >= 0]
    if start_positions:
        text = text[min(start_positions):]

    stop_positions = [text.find(marker) for marker in JD_STOP_MARKERS if text.find(marker) > 0]
    stopped_by_marker = bool(stop_positions)
    if stop_positions:
        text = text[:min(stop_positions)]

    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(line.startswith(prefix) for prefix in JD_NOISE_PREFIXES):
            continue
        if is_recruiter_status(line) or is_recruiter_role(line) or any(marker in line for marker in JD_STOP_MARKERS):
            lines = remove_trailing_recruiter_lines(lines, aggressive=True)
            break
        if line in {"职位描述", "职位详情", "查看全部", "搜索", "一般", "良好", "优秀", "极好"}:
            continue
        line = re.sub(r"\s+", " ", line)
        lines.append(line)

    lines = remove_trailing_recruiter_lines(lines, aggressive=stopped_by_marker)
    cleaned = "\n".join(lines).strip()
    # Some BOSS pages return a mostly single-line body. Remove leading UI fragments.
    for prefix in ("微信扫码分享", "微 信 扫码分享", "举报", "举 报", "职位描述", "职位详情"):
        cleaned = cleaned.removeprefix(prefix).strip()
    return cleaned


def split_tag_text(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [
        part.strip()
        for part in re.split(r"[|｜、,，·]+", str(raw))
        if part.strip()
    ]


def join_tags(*raw_values):
    tags = []
    seen = set()
    for raw in raw_values:
        for tag in split_tag_text(raw):
            key = tag.lower()
            if key not in seen:
                tags.append(tag)
                seen.add(key)
    return " · ".join(tags) if tags else "-"


def pill(text, style="cyan"):
    text = str(text or "").strip()
    if not text or text == "-":
        return ""
    return f"[{style}]● {escape(text)}[/]"


def pill_line(*values, style="cyan"):
    items = [pill(value, style=style) for value in values if str(value or "").strip() and str(value) != "-"]
    return "  ".join(items) if items else "-"


def section(title, body):
    body = str(body or "").strip() or "-"
    return f"[{COLOR_BORDER_SOFT}]{'─' * 18}[/] [bold {COLOR_ACCENT}]{escape(title)}[/]\n{body}"


def divider(label):
    return f"[{COLOR_BORDER_SOFT}]{'─' * 40}[/]\n[{COLOR_MUTED}]{escape(label)}[/]"


def kv(label, value):
    return f"[{COLOR_MUTED}]{escape(label):<4}[/] [{COLOR_TEXT}]{escape(first_nonempty(value, '-'))}[/]"


def meta_line(label, value):
    return f"[{COLOR_MUTED}]{escape(label):<4}[/] [{COLOR_TEXT}]{escape(first_nonempty(value, '-'))}[/]"


def paragraph(text):
    return escape(str(text or "").strip())


CHAT_ASCII_PHRASE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,8})\s+([A-Za-z][A-Za-z0-9+#./-]{1,16})(?![A-Za-z0-9])")


def chat_body_text(text):
    value = re.sub(r"\s+", " ", str(text or "").strip())
    value = CHAT_ASCII_PHRASE_RE.sub(lambda match: f"{match.group(1)}\u00a0{match.group(2)}", value)
    return escape(value)


def chat_hint(text):
    return f"[{COLOR_HINT}]{escape(str(text or '').strip())}[/]"


def chat_message_header(message):
    direction = message.get("direction")
    is_me = direction == "me"
    name = "我" if is_me else "Boss"
    name_style = COLOR_GREEN if is_me else COLOR_ACCENT
    meta = " · ".join(
        escape(str(item).strip())
        for item in (message.get("time"), message.get("status"))
        if str(item or "").strip()
    )
    meta_text = f" [{COLOR_HINT_DIM}]{meta}[/]" if meta else ""
    return f"[bold {name_style}]{name}[/]{meta_text}"


def chat_message_body(message):
    style = COLOR_ME_BODY if message.get("direction") == "me" else COLOR_BOSS_BODY
    return f"[{style}]{chat_body_text(message.get('text', ''))}[/]"


def open_web_action_line():
    return f"[{COLOR_BORDER_SOFT}]{'─' * 28}[/]\n操作  o 打开网页"


def short_link_label(url):
    text = str(url or "").strip()
    if not text or text == "-":
        return "-"
    parsed = urlparse(text)
    path = parsed.path.strip("/")
    if "job_detail/" in path:
        tail = path.split("job_detail/", 1)[1]
        return f"job_detail/{short_text(tail, 28)}"
    return short_text(text, 48)


JD_HEADING_PATTERNS = (
    "岗位职责",
    "工作职责",
    "职位职责",
    "职责描述",
    "岗位要求",
    "任职要求",
    "职位要求",
    "工作能力要求",
    "任职资格",
    "职位描述",
)


def split_numbered_jd_line(line):
    marker = r"(?:\d{1,2}[\.．、](?!\d)|[（(]\d{1,2}[）)])"
    if not re.search(rf"(^|\s){marker}", line):
        return [line]

    parts = []
    matches = list(re.finditer(rf"(?<!\S){marker}", line))
    if not matches:
        return [line]

    if matches[0].start() > 0:
        prefix = line[:matches[0].start()].strip()
        if prefix:
            parts.append(prefix)

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        part = line[match.start():end].strip()
        if part:
            parts.append(part)
    return parts or [line]


def split_jd_sections(jd):
    text = clean_detail_jd(jd)
    if not text:
        return []

    sections = []
    current_title = ""
    current_lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        normalized = line.rstrip(":：")
        is_heading = (
            normalized in JD_HEADING_PATTERNS
            or (
                len(normalized) <= 64
                and re.search(r"(Responsibilities|Requirements|Qualifications|Knowledge|Stability|Performance|Tooling)", normalized, re.I)
            )
        )
        if is_heading:
            if current_lines:
                sections.append((current_title, current_lines))
            current_title = normalized
            current_lines = []
            continue
        split_parts = split_numbered_jd_line(line)
        current_lines.extend(split_parts or [line])

    if current_lines:
        sections.append((current_title, current_lines))
    return sections


def format_jd_sections(jd):
    sections = split_jd_sections(jd)
    if not sections:
        return "详情页未返回可用 JD 正文。"

    rendered = []
    for title, lines in sections:
        if title:
            rendered.append(f"[bold {COLOR_ACCENT}]{escape(title)}[/]")
        for line in lines:
            rendered.append(f"  [{COLOR_FAINT}]•[/] {paragraph(line)}")
        rendered.append("")
    return "\n".join(rendered).strip()


def format_preview_text(job, detail=None, auto_detail=False, loading=False):
    detail = detail or {}
    detail_loaded = bool(detail)
    location = first_nonempty(job.get("location"), "-")
    requirements = first_nonempty(job.get("tags"), "-")
    company_bits = " · ".join(
        item for item in (
            first_nonempty(job.get("boss_name"), "-"),
            job.get("company_scale", ""),
            job.get("company_stage", ""),
            job.get("company_industry", ""),
        )
        if item and item != "-"
    )

    lines = [
        pill_line(
            job.get("salary"),
            boss.district_from_location(job.get("location")),
            job.get("company_scale"),
            style=COLOR_BLUE,
        ),
        "",
        section(
            "岗位概览",
            "\n".join([
                kv("薪资", job.get("salary")),
                kv("公司", company_bits or "-"),
                kv("地点", location),
                kv("要求", requirements),
            ]),
        ),
        "",
        section("技能", pill_line(*split_tag_text(join_tags(job.get("skills"), job.get("job_labels"), detail.get("skill_tags"))), style=COLOR_GREEN)),
        "",
        section("福利", pill_line(*split_tag_text(join_tags(job.get("welfare"))), style=COLOR_SUCCESS)),
        "",
    ]

    if detail_loaded:
        lines.extend([
            section("JD 正文", format_jd_sections(detail.get("jd"))),
            "",
            open_web_action_line(),
        ])
    else:
        detail_hint = "正在加载详情页并提取 JD。" if loading else "按 Enter 打开详情页并提取 JD。"
        if auto_detail and loading:
            detail_hint = "正在自动加载详情页并提取 JD。"
        lines.extend([
            section("详情", f"[{COLOR_MUTED}]{detail_hint}[/]"),
            "",
        ])
        if loading:
            lines.extend([
                f"[{COLOR_FAINT}]•  •  •[/]",
                "",
            ])
        lines.append(open_web_action_line())

    return "\n".join(lines).strip()


def format_chat_text(conversation, chat):
    lines = [
        pill_line("BOSS 会话", "TUI 内回复", style=COLOR_BLUE),
        "",
    ]
    if chat.get("position"):
        lines.extend([section("关联职位", paragraph(chat.get("position"))), ""])

    messages = chat.get("messages") or []
    if not messages:
        preview = first_nonempty(conversation.get("preview"), conversation.get("text"), "")
        if preview and preview != "-":
            lines.extend([
                section("最近消息", f"[{COLOR_BOSS_BODY}]{chat_body_text(preview)}[/]"),
                "",
                section("聊天记录", chat_hint("实时页面暂未返回完整消息列表，正在等待页面渲染。")),
            ])
        else:
            lines.extend([
                section("聊天记录", chat_hint("实时页面暂未返回可读消息，正在等待页面渲染。")),
            ])
    else:
        lines.append(f"[{COLOR_BORDER_SOFT}]{'─' * 18}[/] [bold {COLOR_ACCENT}]聊天记录[/]")
        for message in messages[-20:]:
            lines.append("")
            lines.append(chat_message_header(message))
            lines.append(chat_message_body(message))

    lines.extend([
        "",
        section("回复", chat_hint("底部输入框输入内容，按 Enter 发送。发送前请确认文本准确。")),
    ])
    return "\n".join(lines).strip()


def append_local_message(chat, message, direction="me", time_text="刚刚"):
    """Append a just-sent message when the web DOM has not rendered it yet."""
    chat = dict(chat or {})
    messages = list(chat.get("messages") or [])
    text = str(message or "").strip()
    if not text:
        chat["messages"] = messages
        return chat

    already_visible = any(
        item.get("direction") == direction and str(item.get("text") or "").strip() == text
        for item in messages[-5:]
    )
    if not already_visible:
        messages.append({
            "index": len(messages),
            "direction": direction,
            "time": time_text,
            "text": text,
            "local": True,
        })
    chat["messages"] = messages[-30:]
    return chat


def update_conversation_preview(conversation, message):
    conversation["preview"] = f"我: {short_text(message, 80)}"
    conversation["unread"] = False
    conversation["delivery_status"] = "送达"
    text = str(conversation.get("text") or "")
    if text:
        conversation["text"] = f"{conversation.get('title', '')} {conversation['preview']}".strip()
    return conversation


CONVERSATION_DELIVERY_RE = re.compile(r"\[\s*(已读|送达)\s*\]|(?:^|\s)(已读|送达)(?:\s|$)")
CONVERSATION_TIME_RE = re.compile(r"^(?:今天|昨天|前天|\d{1,2}:\d{2}|\d{1,2}月\d{1,2}日)\s*")
CHAT_SYSTEM_MESSAGE_RE = re.compile(r"(你与该职位竞争者PK情况|查看详细分析|共人投递|优秀竞争者)")


def strip_delivery_status(text):
    return CONVERSATION_DELIVERY_RE.sub(" ", str(text or "")).replace("  ", " ").strip()


def strip_conversation_time_prefix(text):
    return CONVERSATION_TIME_RE.sub("", str(text or "").strip()).strip()


def conversation_status_label(conversation):
    if conversation.get("unread"):
        return "● 未读"
    return str(conversation.get("delivery_status") or "").strip()


def normalize_conversation(conversation):
    conv = dict(conversation or {})
    raw = str(conv.get("text") or "").strip()
    title = str(conv.get("title") or "").strip()
    preview = str(conv.get("preview") or "").strip()
    raw_match = CONVERSATION_DELIVERY_RE.search(raw)
    preview_match = None if raw_match else CONVERSATION_DELIVERY_RE.search(preview)
    match = raw_match or preview_match
    if match:
        delivery_status = next(part for part in match.groups() if part)
        conv["delivery_status"] = delivery_status
        if raw_match:
            before = raw[:match.start()].strip()
            after = raw[match.end():].strip()
            if before:
                title = strip_conversation_time_prefix(strip_delivery_status(before))
            if after:
                preview = strip_delivery_status(after)

    title = strip_conversation_time_prefix(strip_delivery_status(title))
    preview = strip_delivery_status(preview)
    if not title and raw:
        title = strip_conversation_time_prefix(strip_delivery_status(raw[:28]))
    if not preview and raw:
        preview = strip_delivery_status(raw[28:180])

    conv["title"] = short_text(title or "未知联系人", 36)
    conv["preview"] = short_text(preview or "暂无最近消息", 120)
    conv["status_label"] = conversation_status_label(conv)
    return conv


def normalize_chat(chat):
    normalized = dict(chat or {})
    messages = []
    seen = set()
    for message in normalized.get("messages") or []:
        item = dict(message or {})
        text = str(item.get("text") or "").strip()
        if not text or CHAT_SYSTEM_MESSAGE_RE.search(text):
            continue
        match = CONVERSATION_DELIVERY_RE.match(text)
        if match:
            item["direction"] = "me"
            item["status"] = next(part for part in match.groups() if part)
            text = text[match.end():].strip()
        item["text"] = strip_delivery_status(text) if item.get("direction") == "me" else text
        dedupe_key = (str(item.get("time") or "").strip(), item.get("text", ""))
        if not item.get("text") or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        messages.append(item)
    normalized["messages"] = messages
    return normalized


def chat_message_count(chat):
    return sum(1 for message in (chat or {}).get("messages") or [] if str(message.get("text") or "").strip())


def merge_chat_without_losing_history(current_chat, incoming_chat):
    """Do not let a transient empty DOM read erase the visible chat history."""
    incoming = dict(incoming_chat or {})
    current = dict(current_chat or {})
    if not chat_message_count(incoming) and chat_message_count(current):
        merged = dict(current)
        for key in ("header", "position", "canReply", "sendReady", "inputText", "rawText"):
            if key in incoming and incoming.get(key) not in (None, ""):
                merged[key] = incoming.get(key)
        merged["_preserved_messages"] = True
        return merged
    return incoming


def stable_conversation_identity(conversation):
    conv = conversation or {}
    for key in ("dom_key", "user_id", "href", "title"):
        value = str(conv.get(key) or "").strip()
        if value and value != "-":
            return value
    return str(conv.get("index", "")).strip()


def format_send_failure(result):
    reason = result.get("reason", "未知原因")
    debug = result.get("debug") or {}
    input_text = str(debug.get("inputText") or "").strip()
    buttons = debug.get("buttons") or []
    parts = [reason]
    if input_text:
        parts.append(f"输入框已有 {len(input_text)} 字")
    if buttons:
        labels = []
        for button in buttons[:3]:
            label = str(button.get("text") or button.get("className") or button.get("tag") or "").strip()
            if label:
                labels.append(short_text(label, 18))
        if labels:
            parts.append("可见按钮: " + " / ".join(labels))
    return "；".join(parts)


def default_greeting_message(job):
    title = str(job.get("title") or "这个岗位").strip()
    company = str(job.get("boss_name") or "").strip()
    target = f"{company}的{title}" if company else title
    return f"您好，我对{target}很感兴趣，想进一步了解岗位职责和团队情况，方便沟通一下吗？"


FETCH_CONVERSATIONS_JS = r"""
(function(){
  function text(el) {
    return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  }
  function lines(el) {
    return (el && (el.innerText || el.textContent || '') || '')
      .split(/\n+/)
      .map(function(line){ return line.replace(/\s+/g, ' ').trim(); })
      .filter(Boolean);
  }
  function firstText(root, selectors) {
    for (var i = 0; i < selectors.length; i++) {
      var el = root.querySelector(selectors[i]);
      var value = el ? text(el) : '';
      if (value) return value;
    }
    return '';
  }
  function firstAttr(root, names) {
    var nodes = [root].concat(Array.from(root.querySelectorAll('a,[data-uid],[data-user-id],[data-id],[data-key],[ka]')));
    for (var i = 0; i < nodes.length; i++) {
      for (var j = 0; j < names.length; j++) {
        var value = nodes[i].getAttribute && nodes[i].getAttribute(names[j]);
        if (value) return names[j] + ':' + value;
      }
      if (nodes[i].href) return 'href:' + nodes[i].href;
    }
    return '';
  }
  return JSON.stringify(Array.from(document.querySelectorAll('.chat-content li'))
    .filter(function(el){
      var rect = el.getBoundingClientRect();
      return !!(rect.width || rect.height);
    })
    .map(function(el, index){
      var rect = el.getBoundingClientRect();
      var raw = text(el);
      var rowLines = lines(el);
      var title = firstText(el, [
        '.friend-name',
        '[class*="friend-name"]',
        '[class*="geek-name"]',
        '[class*="boss-name"]',
        '[class*="user-name"]',
        '[class*="name"]'
      ]) || rowLines[0] || raw.slice(0, 28);
      var preview = firstText(el, [
        '.last-msg',
        '[class*="last-msg"]',
        '[class*="lastMsg"]',
        '[class*="preview"]',
        '[class*="message"]',
        '[class*="content"]'
      ]);
      if (!preview) {
        preview = rowLines.filter(function(line){ return line !== title; }).join(' ');
      }
      if (!preview) preview = raw.slice(title.length, 180);
      var unread = !!el.querySelector('.unread, .badge, [class*=unread], [class*=badge]');
      return {
        index: index,
        dom_key: firstAttr(el, ['data-uid', 'data-user-id', 'data-id', 'data-key', 'ka', 'href']),
        text: raw,
        title: title,
        preview: preview,
        unread: unread,
        rect: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)]
      };
    }));
})()
"""


FETCH_CURRENT_CHAT_JS = r"""
(function(){
  function text(el) {
    return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  }
  function lines(el) {
    return (el && (el.innerText || el.textContent || '') || '')
      .split(/\n+/)
      .map(function(line){ return line.replace(/\s+/g, ' ').trim(); })
      .filter(Boolean);
  }
  function visible(el) {
    if (!el) return false;
    var rect = el.getBoundingClientRect();
    return !!(rect.width || rect.height);
  }
  function findInput() {
    var inputs = Array.from(document.querySelectorAll(
      '.chat-input[contenteditable], [contenteditable=true], [contenteditable=""], textarea, input[type=text]'
    )).filter(function(el){
      var className = el.className || '';
      return visible(el)
        && !el.disabled
        && !el.readOnly
        && className.indexOf('boss-search-input') < 0
        && className.indexOf('ipt-search') < 0;
    });
    return inputs.find(function(el){ return (el.className || '').indexOf('chat-input') >= 0; }) || inputs[0];
  }
  function findSendButton() {
    return Array.from(document.querySelectorAll('.btn-send, button, [role=button], a'))
      .find(function(el){
        var label = text(el);
        var className = el.className || '';
        return visible(el)
          && !el.disabled
          && className.indexOf('disabled') < 0
          && (/发送|Send/i.test(label) || className.indexOf('btn-send') >= 0);
      });
  }
  function messageDirection(el) {
    var cursor = el;
    while (cursor && cursor !== document.body) {
      var className = cursor.className || '';
      if (className.indexOf('item-me') >= 0 || className.indexOf('item-myself') >= 0 || className.indexOf('myself') >= 0) return 'me';
      if (className.indexOf('item-friend') >= 0 || className.indexOf('item-boss') >= 0 || className.indexOf('friend') >= 0) return 'boss';
      cursor = cursor.parentElement;
    }
    return 'boss';
  }
  function uniqueMessages(items) {
    var seen = {};
    return items.filter(function(item){
      var key = item.direction + '|' + item.time + '|' + item.text;
      if (!item.text || seen[key]) return false;
      seen[key] = true;
      return true;
    });
  }
  function fallbackMessages(root) {
    var skip = /^(BOSS\s*会话|TUI\s*内回复|关联职位|聊天记录|回复|查看职位|发送|常用语|表情|图片|附件|请输入|输入回复|按 Enter|当前会话|实时页面|暂无可读消息|在线|刚刚活跃|今日活跃|本周活跃|本月活跃)$/;
    return lines(root).filter(function(line){
      if (skip.test(line)) return false;
      if (/^(对方正在输入|已读|送达)$/.test(line)) return false;
      if (/^(沟通职位|查看简历|交换微信|电话沟通|视频面试)/.test(line)) return false;
      return line.length > 0;
    }).slice(-30).map(function(line, index){
      return {
        index: index,
        direction: /^(我|已读|送达)\b/.test(line) ? 'me' : 'boss',
        time: '',
        text: line.replace(/^我\s*/, '')
      };
    });
  }
  function firstElement(root, selectors) {
    for (var i = 0; i < selectors.length; i++) {
      var el = root.querySelector(selectors[i]);
      if (el && visible(el)) return el;
    }
    return null;
  }
  var root = document.querySelector('.chat-conversation')
    || document.querySelector('[class*="chat-conversation"]')
    || document.querySelector('[class*="conversation"]')
    || document.querySelector('.chat-content')
    || document.body;
  var messageRoot = firstElement(root, [
    '.message-list',
    '.chat-message-list',
    '.chat-record',
    '.chat-record-list',
    '[class*="message-list"]',
    '[class*="messageList"]',
    '[class*="message-box"]',
    '[class*="chat-record"]',
    '[class*="record-list"]'
  ]) || root;
  var header = text(firstElement(root, ['.chat-header', '[class*="chat-header"]']) || root);
  var position = text(document.querySelector('.chat-position-content, [class*="chat-position"]'));
  var messageSelector = [
    '.message-item',
    '.message-card',
    '.chat-message',
    '.msg-item',
    '.dialog-item',
    '[class*="message-item"]',
    '[class*="messageItem"]',
    '[class*="message-card"]',
    '[class*="chat-message"]',
    '[class*="msg-item"]',
    '[class*="dialog-item"]',
    '[class*="item-myself"]',
    '[class*="item-me"]',
    '[class*="item-friend"]',
    '[class*="item-boss"]'
  ].join(',');
  var messageEls = Array.from(messageRoot.querySelectorAll(messageSelector)).filter(visible);
  var messages = uniqueMessages(messageEls.map(function(el, index){
    var direction = messageDirection(el);
    var timeEl = el.querySelector('.item-time, [class*="item-time"], [class*="time"]');
    var contentEl = firstElement(el, [
      '.message-content',
      '.message-text',
      '.bubble-content',
      '.text',
      '[class*="message-content"]',
      '[class*="messageText"]',
      '[class*="message-text"]',
      '[class*="bubble"]',
      '[class*="content"]'
    ]);
    return {
      index: index,
      direction: direction,
      time: timeEl ? text(timeEl) : '',
      text: contentEl ? text(contentEl) : text(el)
    };
  })).filter(function(item){ return item.text; });
  if (!messages.length) messages = fallbackMessages(messageRoot);
  var input = findInput();
  var send = findSendButton();
  return JSON.stringify({
    header: header,
    position: position,
    messages: messages.slice(-30),
    canReply: !!input,
    sendReady: !!send,
    inputText: input ? text(input) : '',
    rawText: text(root).slice(0, 2000)
  });
})()
"""


CHAT_PAGE_READY_JS = r"""
(function(){
  function visible(el) {
    if (!el) return false;
    var rect = el.getBoundingClientRect();
    return !!(rect.width || rect.height);
  }
  var rows = Array.from(document.querySelectorAll('.chat-content li, .user-list-content li'))
    .filter(visible);
  var input = Array.from(document.querySelectorAll(
    '.chat-input[contenteditable], [contenteditable=true], [contenteditable=""], textarea, input[type=text]'
  )).find(visible);
  return JSON.stringify({
    ready: rows.length > 0 || !!input || !!document.querySelector('.chat-conversation'),
    rows: rows.length,
    hasInput: !!input,
    title: document.title || ''
  });
})()
"""


CURRENT_CHAT_READY_JS = r"""
(function(){
  function visible(el) {
    if (!el) return false;
    var rect = el.getBoundingClientRect();
    return !!(rect.width || rect.height);
  }
  function lines(el) {
    return (el && (el.innerText || el.textContent || '') || '')
      .split(/\n+/)
      .map(function(line){ return line.replace(/\s+/g, ' ').trim(); })
      .filter(Boolean);
  }
  var messageSelector = [
    '.message-item',
    '.message-card',
    '.chat-message',
    '.msg-item',
    '.dialog-item',
    '[class*="message-item"]',
    '[class*="messageItem"]',
    '[class*="message-card"]',
    '[class*="chat-message"]',
    '[class*="msg-item"]',
    '[class*="dialog-item"]',
    '[class*="item-myself"]',
    '[class*="item-me"]',
    '[class*="item-friend"]',
    '[class*="item-boss"]'
  ].join(',');
  var messages = Array.from(document.querySelectorAll(messageSelector)).filter(visible);
  var input = Array.from(document.querySelectorAll(
    '.chat-input[contenteditable], [contenteditable=true], [contenteditable=""], textarea, input[type=text]'
  )).find(visible);
  var header = document.querySelector('.chat-conversation .chat-header, .chat-conversation');
  var root = document.querySelector('.chat-conversation') || document.querySelector('.chat-content') || document.body;
  var fallbackLines = lines(root).filter(function(line){
    return !/^(BOSS\s*会话|TUI\s*内回复|关联职位|聊天记录|回复|查看职位|发送|常用语|表情|图片|附件|请输入|输入回复|在线|刚刚活跃|今日活跃|本周活跃|本月活跃)$/.test(line);
  });
  return JSON.stringify({
    ready: messages.length > 0 || fallbackLines.length > 1,
    messages: messages.length,
    fallbackLines: fallbackLines.length,
    hasInput: !!input,
    hasHeader: !!header
  });
})()
"""


CLICK_CONVERSATION_JS_TEMPLATE = r"""
(function(){
  var index = __INDEX__;
  var rows = Array.from(document.querySelectorAll('.chat-content li, .user-list-content li'))
    .filter(function(el){
      var rect = el.getBoundingClientRect();
      return !!(rect.width || rect.height);
    });
  var row = rows[index];
  if (!row) return JSON.stringify({clicked: false, reason: 'conversation not found'});
  var el = row.querySelector('.friend-content') || row;
  var rect = el.getBoundingClientRect();
  var before = (document.querySelector('.chat-conversation') || document.body).innerText || '';
  ['pointerdown', 'mousedown', 'mouseup', 'pointerup', 'click'].forEach(function(type){
    try {
      el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
    } catch (e) {}
  });
  return JSON.stringify({
    clicked: true,
    x: Math.round(rect.left + Math.min(40, rect.width / 2)),
    y: Math.round(rect.top + Math.min(28, rect.height / 2)),
    text: (el.innerText || '').replace(/\s+/g, ' ').trim(),
    changed: before !== ((document.querySelector('.chat-conversation') || document.body).innerText || '')
  });
})()
"""


PREPARE_MESSAGE_INPUT_JS = r"""
(function(){
  function visible(el) {
    if (!el) return false;
    var rect = el.getBoundingClientRect();
    return !!(rect.width || rect.height);
  }
  function text(el) {
    return (el.innerText || el.textContent || el.value || '').replace(/\s+/g, ' ').trim();
  }
  var inputs = Array.from(document.querySelectorAll(
    '.chat-input[contenteditable], [contenteditable=true], [contenteditable=""], textarea, input[type=text]'
  )).filter(function(el){
    var className = el.className || '';
    return visible(el)
      && !el.disabled
      && !el.readOnly
      && className.indexOf('boss-search-input') < 0
      && className.indexOf('ipt-search') < 0;
  });
  var input = inputs.find(function(el){ return (el.className || '').indexOf('chat-input') >= 0; }) || inputs[0];
  if (!input) {
    return JSON.stringify({
      ready: false,
      reason: 'chat input not found',
      inputs: inputs.slice(0, 5).map(function(el){ return {tag: el.tagName, className: el.className || '', text: text(el)}; })
    });
  }
  input.focus();
  if ('value' in input) {
    input.value = '';
  } else {
    input.innerHTML = '';
    input.textContent = '';
  }
  input.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward', data: null}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
  return JSON.stringify({ready: true, inputTag: input.tagName, inputClass: input.className || ''});
})()
"""


MESSAGE_INPUT_READY_JS = r"""
(function(){
  function visible(el) {
    if (!el) return false;
    var rect = el.getBoundingClientRect();
    return !!(rect.width || rect.height);
  }
  function text(el) {
    return (el.innerText || el.textContent || el.value || '').replace(/\s+/g, ' ').trim();
  }
  var inputs = Array.from(document.querySelectorAll(
    '.chat-input[contenteditable], [contenteditable=true], [contenteditable=""], textarea, input[type=text]'
  )).filter(function(el){
    var className = el.className || '';
    return visible(el)
      && !el.disabled
      && !el.readOnly
      && className.indexOf('boss-search-input') < 0
      && className.indexOf('ipt-search') < 0;
  });
  var input = inputs.find(function(el){ return (el.className || '').indexOf('chat-input') >= 0; }) || inputs[0];
  return JSON.stringify({
    ready: !!input,
    inputText: input ? text(input) : '',
    inputTag: input ? input.tagName : '',
    inputClass: input ? (input.className || '') : ''
  });
})()
"""


SET_MESSAGE_INPUT_JS_TEMPLATE = r"""
(function(){
  var expected = __TEXT__;
  function visible(el) {
    if (!el) return false;
    var rect = el.getBoundingClientRect();
    return !!(rect.width || rect.height);
  }
  function text(el) {
    return (el.innerText || el.textContent || el.value || '').replace(/\s+/g, ' ').trim();
  }
  var inputs = Array.from(document.querySelectorAll(
    '.chat-input[contenteditable], [contenteditable=true], [contenteditable=""], textarea, input[type=text]'
  )).filter(function(el){
    var className = el.className || '';
    return visible(el)
      && !el.disabled
      && !el.readOnly
      && className.indexOf('boss-search-input') < 0
      && className.indexOf('ipt-search') < 0;
  });
  var input = inputs.find(function(el){ return (el.className || '').indexOf('chat-input') >= 0; }) || inputs[0];
  if (!input) {
    return JSON.stringify({ready: false, reason: 'chat input not found'});
  }
  input.focus();
  if ('value' in input) {
    input.value = expected;
  } else {
    input.innerHTML = '';
    input.textContent = '';
    try {
      var range = document.createRange();
      range.selectNodeContents(input);
      range.collapse(false);
      var selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      document.execCommand('insertText', false, expected);
    } catch (e) {}
    if (text(input) !== expected) {
      input.textContent = expected;
      input.innerText = expected;
    }
  }
  try {
    input.dispatchEvent(new InputEvent('beforeinput', {bubbles: true, cancelable: true, inputType: 'insertText', data: expected}));
  } catch (e) {}
  input.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: expected}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
  input.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: expected.slice(-1) || 'a'}));
  return JSON.stringify({
    ready: true,
    inputText: text(input),
    inputTag: input.tagName,
    inputClass: input.className || ''
  });
})()
"""


SEND_PREPARED_MESSAGE_JS_TEMPLATE = r"""
(function(){
  var expected = __TEXT__;
  function visible(el) {
    if (!el) return false;
    var rect = el.getBoundingClientRect();
    return !!(rect.width || rect.height);
  }
  function text(el) {
    return (el.innerText || el.textContent || el.value || '').replace(/\s+/g, ' ').trim();
  }
  var input = Array.from(document.querySelectorAll(
    '.chat-input[contenteditable], [contenteditable=true], [contenteditable=""], textarea, input[type=text]'
  )).find(function(el){
    var className = el.className || '';
    return visible(el)
      && !el.disabled
      && !el.readOnly
      && className.indexOf('boss-search-input') < 0
      && className.indexOf('ipt-search') < 0;
  });
  var inputText = input ? text(input) : '';
  var buttons = Array.from(document.querySelectorAll('.btn-send, button, [role=button], a'))
    .filter(function(el){ return visible(el); });
  var send = buttons.find(function(el){
    var label = text(el);
    var className = el.className || '';
    return !el.disabled
      && className.indexOf('disabled') < 0
      && (/发送|Send/i.test(label) || className.indexOf('btn-send') >= 0);
  });
  if (!send) {
    return JSON.stringify({
      ready: false,
      reason: 'send button disabled or not found',
      inputText: inputText,
      buttons: buttons.slice(0, 8).map(function(el){ return {tag: el.tagName, text: text(el), className: el.className || ''}; })
    });
  }
  var rect = send.getBoundingClientRect();
  return JSON.stringify({
    ready: true,
    inputText: inputText,
    buttonText: text(send),
    buttonClass: send.className || '',
    x: Math.round(rect.left + rect.width / 2),
    y: Math.round(rect.top + rect.height / 2)
  });
})()
"""


CLICK_SEND_BUTTON_JS = r"""
(function(){
  function visible(el) {
    if (!el) return false;
    var rect = el.getBoundingClientRect();
    return !!(rect.width || rect.height);
  }
  function text(el) {
    return (el.innerText || el.textContent || el.value || '').replace(/\s+/g, ' ').trim();
  }
  var send = Array.from(document.querySelectorAll('.btn-send, button, [role=button], a'))
    .find(function(el){
      var label = text(el);
      var className = el.className || '';
      return visible(el)
        && !el.disabled
        && className.indexOf('disabled') < 0
        && (/发送|Send/i.test(label) || className.indexOf('btn-send') >= 0);
    });
  if (!send) {
    return JSON.stringify({clicked: false, reason: 'send button not found'});
  }
  ['pointerdown', 'mousedown', 'mouseup', 'pointerup', 'click'].forEach(function(type){
    try {
      send.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
    } catch (e) {}
  });
  return JSON.stringify({clicked: true, text: text(send), className: send.className || ''});
})()
"""


VERIFY_MESSAGE_SENT_JS_TEMPLATE = r"""
(function(){
  var expected = __TEXT__;
  function text(el) {
    return (el.innerText || el.textContent || el.value || '').replace(/\s+/g, ' ').trim();
  }
  function visible(el) {
    if (!el) return false;
    var rect = el.getBoundingClientRect();
    return !!(rect.width || rect.height);
  }
  var messages = Array.from(document.querySelectorAll('.message-item')).map(function(el){
    var className = el.className || '';
    var direction = (className.indexOf('item-me') >= 0 || className.indexOf('item-myself') >= 0) ? 'me' : 'boss';
    var contentEl = el.querySelector('.message-content');
    return {direction: direction, text: contentEl ? text(contentEl) : text(el)};
  }).filter(function(item){ return item.text; });
  function messageBody(value) {
    return String(value || '')
      .replace(/^\d{1,2}:\d{2}\s*/, '')
      .replace(/^(送达|已读)\s*/, '')
      .trim();
  }
  var matched = messages.slice(-5).some(function(item){
    return item.direction === 'me' && messageBody(item.text) === expected;
  });
  var input = Array.from(document.querySelectorAll(
    '.chat-input[contenteditable], [contenteditable=true], [contenteditable=""], textarea, input[type=text]'
  )).find(function(el){
    var className = el.className || '';
    return visible(el)
      && !el.disabled
      && !el.readOnly
      && className.indexOf('boss-search-input') < 0
      && className.indexOf('ipt-search') < 0;
  });
  var inputText = input ? text(input) : '';
  return JSON.stringify({
    sent: matched,
    verified: matched,
    inputCleared: inputText === '',
    inputText: inputText,
    lastMessage: messages.length ? messages[messages.length - 1].text : ''
  });
})()
"""


START_GREETING_JS = r"""
(function(){
  function visible(el) {
    if (!el) return false;
    var rect = el.getBoundingClientRect();
    return !!(rect.width || rect.height);
  }
  var button = Array.from(document.querySelectorAll('.btn-startchat, .btn-startchat-wrap, a, button'))
    .find(function(el){
      return visible(el) && /立即沟通|继续沟通|沟通/.test((el.innerText || el.textContent || '').trim());
    });
  var link = button && (button.matches && button.matches('a') ? button : button.querySelector('a'));
  if (!link) return JSON.stringify({ready: false, reason: 'start chat link not found'});
  var api = link.getAttribute('data-url') || '';
  var redirect = link.getAttribute('redirect-url') || '';
  var result = {
    ready: true,
    clicked: true,
    text: (link.innerText || link.textContent || '').replace(/\s+/g, ' ').trim(),
    api: api,
    redirect: redirect
  };
  if (api) {
    try {
      var xhr = new XMLHttpRequest();
      xhr.open('GET', api, false);
      xhr.setRequestHeader('x-requested-with', 'XMLHttpRequest');
      xhr.send(null);
      result.status = xhr.status;
      result.responseText = (xhr.responseText || '').slice(0, 1200);
    } catch (e) {
      result.error = String(e && e.message || e);
    }
  }
  return JSON.stringify({
    ready: result.ready,
    clicked: result.clicked,
    text: result.text,
    api: result.api,
    redirect: result.redirect,
    status: result.status,
    responseText: result.responseText,
    error: result.error
  });
})()
"""


class LiveCache:
    """Small SQLite cache for visited TUI pages/details. Expiry controls freshness."""

    def __init__(self, path=None):
        self.path = path or DEFAULT_SQLITE_CACHE_PATH
        self.lock = threading.RLock()
        self.enabled = True
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=3000")
            self._init_schema()
        except sqlite3.Error:
            self.enabled = False
            self.conn = None

    def _init_schema(self):
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS page_cache (
                    cache_key TEXT PRIMARY KEY,
                    fetched_at REAL NOT NULL,
                    keyword TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    filters_json TEXT NOT NULL,
                    page INTEGER NOT NULL,
                    jobs_json TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS detail_cache (
                    cache_key TEXT PRIMARY KEY,
                    fetched_at REAL NOT NULL,
                    title TEXT,
                    company TEXT,
                    job_link TEXT,
                    detail_json TEXT NOT NULL
                )
                """
            )

    def close(self):
        if self.enabled and self.conn:
            self.conn.close()
            self.conn = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _expired(self, fetched_at, ttl):
        return time.time() - float(fetched_at or 0) > ttl

    def page_key(self, keyword, city_code, filters, page):
        payload = {
            "v": 1,
            "keyword": keyword,
            "city_code": city_code,
            "filters": filters or {},
            "page": int(page),
            "page_size": PAGE_SIZE,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()

    def get_page(self, keyword, city_code, filters, page, ttl=PAGE_SQLITE_CACHE_TTL_SECONDS):
        if not self.enabled:
            return None
        key = self.page_key(keyword, city_code, filters, page)
        with self.lock:
            row = self.conn.execute(
                "SELECT fetched_at, jobs_json FROM page_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
            if not row:
                return None
            fetched_at, jobs_json = row
            if self._expired(fetched_at, ttl):
                self.conn.execute("DELETE FROM page_cache WHERE cache_key = ?", (key,))
                self.conn.commit()
                return None
            jobs = json.loads(jobs_json)
            for job in jobs:
                job["_cache_hit"] = True
                job["_cached_at"] = fetched_at
            return jobs

    def put_page(self, keyword, city_code, filters, page, jobs):
        if not self.enabled:
            return
        key = self.page_key(keyword, city_code, filters, page)
        filters_json = json.dumps(filters or {}, ensure_ascii=False, sort_keys=True)
        clean_jobs = []
        for job in jobs or []:
            item = dict(job)
            item.pop("_cache_hit", None)
            item.pop("_cached_at", None)
            clean_jobs.append(item)
        jobs_json = json.dumps(clean_jobs, ensure_ascii=False)
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO page_cache
                    (cache_key, fetched_at, keyword, city_code, filters_json, page, jobs_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (key, time.time(), keyword, city_code, filters_json, int(page), jobs_json),
            )

    def get_detail(self, cache_key, ttl=DETAIL_SQLITE_CACHE_TTL_SECONDS):
        if not self.enabled or not cache_key:
            return None
        with self.lock:
            row = self.conn.execute(
                "SELECT fetched_at, detail_json FROM detail_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if not row:
                return None
            fetched_at, detail_json = row
            if self._expired(fetched_at, ttl):
                self.conn.execute("DELETE FROM detail_cache WHERE cache_key = ?", (cache_key,))
                self.conn.commit()
                return None
            detail = json.loads(detail_json)
            detail["_cache_hit"] = True
            detail["_cache_source"] = "sqlite"
            detail["_cached_at"] = fetched_at
            return detail

    def put_detail(self, cache_key, job, detail):
        if not self.enabled or not cache_key or not detail:
            return
        stored = dict(detail)
        stored.pop("_cache_hit", None)
        stored.pop("_cache_source", None)
        stored.pop("_cached_at", None)
        detail_json = json.dumps(stored, ensure_ascii=False)
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO detail_cache
                    (cache_key, fetched_at, title, company, job_link, detail_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    time.time(),
                    job.get("title", ""),
                    job.get("boss_name", ""),
                    job.get("job_link", ""),
                    detail_json,
                ),
            )


class LiveBossClient:
    """Small CDP client for realtime list/detail reads. It never reads local result files."""

    def __init__(self, keyword, city, filters=None, cdp_port=boss.DEFAULT_CDP_PORT):
        self.keyword = keyword
        self.city_name, self.city_code = boss.resolve_city(city)
        self.filters = filters or {}
        self.cdp = boss.CDPSession(cdp_port)
        self.target_id = None
        self.session_id = None
        self.chat_target_id = None
        self.chat_session_id = None
        self.detail_cache = {}
        self.detail_cache_ttl = DETAIL_CACHE_TTL_SECONDS
        self.detail_fetch_lock = threading.Lock()
        self.detail_last_request_at = 0
        self.detail_risk_pause_until = 0
        self._open_search_target()

    def _response_error(self, response):
        error = (response or {}).get("error") or {}
        message = error.get("message") or error.get("data")
        return str(message or response or "unknown CDP response")

    def _create_target(self, url, background=True, allow_foreground_fallback=False):
        params = {"url": url}
        if background:
            params["background"] = True
        response = self.cdp.send("Target.createTarget", params)
        target_id = response.get("result", {}).get("targetId")
        if target_id:
            return target_id

        if background and allow_foreground_fallback:
            response = self.cdp.send("Target.createTarget", {"url": url})
            target_id = response.get("result", {}).get("targetId")
            if target_id:
                return target_id

        raise RuntimeError(f"创建浏览器标签失败: {self._response_error(response)}")

    def _attach_target(self, target_id):
        response = self.cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = response.get("result", {}).get("sessionId")
        if not session_id:
            raise RuntimeError(f"连接浏览器标签失败: {self._response_error(response)}")
        return session_id

    def _open_search_target(self):
        url = boss.build_search_url(self.keyword, self.city_code, 1, self.filters)
        self.target_id = self._create_target(url, background=True)
        self.session_id = self._attach_target(self.target_id)
        time.sleep(2)

    def close(self):
        try:
            if self.chat_target_id:
                self.cdp.send("Target.closeTarget", {"targetId": self.chat_target_id}, timeout=5)
            if self.target_id:
                self.cdp.send("Target.closeTarget", {"targetId": self.target_id}, timeout=5)
        finally:
            self.cdp.close()

    def fetch_page(self, page):
        params = {
            "scene": "1",
            "query": self.keyword,
            "city": self.city_code,
            "page": page,
            "pageSize": PAGE_SIZE,
        }
        for key, value in self.filters.items():
            if value:
                params[key] = value

        api_url = f"{boss.API_JOB_LIST_PATH}?{urlencode(params)}"
        api_js = boss.FETCH_API_JS_TEMPLATE.replace("__API_URL__", api_url)
        jobs = boss.parse_api_jobs_eval_value(self.cdp.eval_js(api_js, self.session_id))
        for job in jobs:
            key = job.get("job_link") or job.get("title", "")
            job["job_id"] = hashlib.md5(key.encode()).hexdigest()[:16]
            job["_live_page"] = page
        return jobs

    def detail_cache_key(self, job):
        return str(job.get("job_id") or job.get("job_link") or "").strip()

    def get_cached_detail(self, job):
        key = self.detail_cache_key(job)
        if not key:
            return None
        cached = self.detail_cache.get(key)
        if not cached:
            return None
        if time.time() - cached["ts"] > self.detail_cache_ttl:
            self.detail_cache.pop(key, None)
            return None
        detail = dict(cached["detail"] or {})
        detail["_cache_hit"] = True
        detail["_cached_at"] = cached["ts"]
        return detail

    def put_cached_detail(self, job, detail):
        key = self.detail_cache_key(job)
        if not key or not detail:
            return detail
        stored = dict(detail)
        stored.pop("_cache_hit", None)
        stored.pop("_cached_at", None)
        self.detail_cache[key] = {
            "ts": time.time(),
            "detail": stored,
        }
        return detail

    def fetch_detail(self, job):
        if not job.get("job_link"):
            return None
        cached = self.get_cached_detail(job)
        if cached:
            return cached

        with self.detail_fetch_lock:
            cached = self.get_cached_detail(job)
            if cached:
                return cached

            now = time.time()
            if self.detail_risk_pause_until > now:
                remaining = int(self.detail_risk_pause_until - now)
                raise RuntimeError(f"检测到验证码/风控页，已暂停自动详情请求 {remaining}s")

            elapsed = now - self.detail_last_request_at
            if elapsed < DETAIL_MIN_INTERVAL_SECONDS:
                time.sleep(DETAIL_MIN_INTERVAL_SECONDS - elapsed)

            target_id = self._create_target("about:blank", background=True)
            sid = self._attach_target(target_id)
            try:
                self.detail_last_request_at = time.time()
                self.cdp.send("Page.navigate", {"url": boss.build_detail_url(job)}, sid)
                time.sleep(DETAIL_WAIT_SECONDS)
                value = self.cdp.eval_js(boss.EXTRACT_DETAIL_JS, sid)
                try:
                    extracted = json.loads(value) if isinstance(value, str) else {"jd": "", "tags": []}
                except (json.JSONDecodeError, ValueError, TypeError):
                    extracted = {"jd": "", "tags": []}
                if looks_like_risk_page((extracted or {}).get("jd", "")):
                    self.detail_risk_pause_until = time.time() + DETAIL_RISK_PAUSE_SECONDS
                    raise RuntimeError("检测到验证码/安全验证页，已暂停自动详情请求")
                detail = boss.build_detail_record(job, extracted)
                return self.put_cached_detail(job, detail)
            finally:
                self.cdp.send("Target.closeTarget", {"targetId": target_id}, timeout=5)

    def build_agent_context(self, job, detail=None):
        return build_agent_context(self.keyword, self.city_name, self.filters, job, detail)

    def ensure_chat_target(self):
        if self.chat_session_id:
            return
        self.chat_target_id = self._create_target("https://www.zhipin.com/web/geek/chat", background=True)
        self.chat_session_id = self._attach_target(self.chat_target_id)
        self._wait_for_chat_page_ready(self.chat_session_id)

    def _wait_until_js_ready(self, sid, js, timeout=CHAT_READY_WAIT_SECONDS):
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            try:
                raw = self.cdp.eval_js(js, sid)
                last = json.loads(raw) if isinstance(raw, str) else {}
            except (json.JSONDecodeError, ValueError, TypeError):
                last = {}
            if not isinstance(last, dict):
                return {"ready": True, "rawType": type(last).__name__}
            if last.get("ready"):
                return last
            time.sleep(CHAT_POLL_INTERVAL_SECONDS)
        return last

    def _wait_for_chat_page_ready(self, sid):
        return self._wait_until_js_ready(sid, CHAT_PAGE_READY_JS, timeout=CHAT_READY_WAIT_SECONDS)

    def _wait_for_current_chat_ready(self, sid):
        return self._wait_until_js_ready(sid, CURRENT_CHAT_READY_JS, timeout=CONVERSATION_OPEN_WAIT_SECONDS)

    def fetch_conversations(self):
        self.ensure_chat_target()
        raw = self.cdp.eval_js(FETCH_CONVERSATIONS_JS, self.chat_session_id)
        try:
            conversations = json.loads(raw) if isinstance(raw, str) else []
        except (json.JSONDecodeError, ValueError, TypeError):
            conversations = []
        normalized = []
        for conv in conversations:
            conv = normalize_conversation(conv)
            key = stable_conversation_identity(conv)
            conv["conversation_id"] = hashlib.md5(key.encode()).hexdigest()[:16]
            normalized.append(conv)
        return normalized

    def open_conversation(self, index):
        self.ensure_chat_target()
        js = CLICK_CONVERSATION_JS_TEMPLATE.replace("__INDEX__", str(int(index)))
        raw_clicked = self.cdp.eval_js(js, self.chat_session_id)
        try:
            clicked = json.loads(raw_clicked) if isinstance(raw_clicked, str) else {}
        except (json.JSONDecodeError, ValueError, TypeError):
            clicked = {}
        if clicked.get("clicked"):
            self._wait_for_current_chat_ready(self.chat_session_id)
        elif clicked.get("x") is not None and clicked.get("y") is not None:
            x = clicked.get("x")
            y = clicked.get("y")
            self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, self.chat_session_id)
            self.cdp.send(
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
                self.chat_session_id,
            )
            self.cdp.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
                self.chat_session_id,
            )
            self._wait_for_current_chat_ready(self.chat_session_id)
        return clicked

    def _click_point(self, sid, x, y):
        self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, sid)
        self.cdp.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
            sid,
        )
        self.cdp.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
            sid,
        )

    def _press_enter(self, sid):
        for event_type in ("keyDown", "keyUp"):
            self.cdp.send(
                "Input.dispatchKeyEvent",
                {
                    "type": event_type,
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                    "unmodifiedText": "\r",
                    "text": "\r" if event_type == "keyDown" else "",
                },
                sid,
            )

    def _verify_message_sent(self, sid, text):
        verify_js = VERIFY_MESSAGE_SENT_JS_TEMPLATE.replace("__TEXT__", json.dumps(text, ensure_ascii=False))
        last = {}
        for _ in range(6):
            raw_verify = self.cdp.eval_js(verify_js, sid)
            try:
                last = json.loads(raw_verify) if isinstance(raw_verify, str) else {}
            except (json.JSONDecodeError, ValueError, TypeError):
                last = {"sent": False, "reason": "invalid send verify result"}
            if last.get("sent"):
                return last
            time.sleep(0.5)
        return last or {"sent": False, "reason": "message not visible after send"}

    def _wait_for_message_input(self, sid, timeout=8):
        deadline = time.time() + timeout
        last = {"ready": False, "reason": "chat input not ready"}
        while time.time() < deadline:
            raw_ready = self.cdp.eval_js(MESSAGE_INPUT_READY_JS, sid)
            try:
                last = json.loads(raw_ready) if isinstance(raw_ready, str) else {}
            except (json.JSONDecodeError, ValueError, TypeError):
                last = {"ready": False, "reason": "invalid input ready result"}
            if last.get("ready"):
                return last
            time.sleep(0.5)
        return last or {"ready": False, "reason": "chat input not ready"}

    def _force_set_message_input(self, sid, text):
        set_js = SET_MESSAGE_INPUT_JS_TEMPLATE.replace("__TEXT__", json.dumps(text, ensure_ascii=False))
        raw_set = self.cdp.eval_js(set_js, sid)
        try:
            return json.loads(raw_set) if isinstance(raw_set, str) else {}
        except (json.JSONDecodeError, ValueError, TypeError):
            return {"ready": False, "reason": "invalid input set result"}

    def _read_send_button(self, sid, text):
        send_js = SEND_PREPARED_MESSAGE_JS_TEMPLATE.replace("__TEXT__", json.dumps(text, ensure_ascii=False))
        raw_send = self.cdp.eval_js(send_js, sid)
        try:
            return json.loads(raw_send) if isinstance(raw_send, str) else {}
        except (json.JSONDecodeError, ValueError, TypeError):
            return {"ready": False, "reason": "invalid send button result"}

    def _wait_for_send_button(self, sid, text, timeout=4):
        attempts = max(1, int(timeout / 0.4))
        last = {"ready": False, "reason": "send button not ready"}
        for _ in range(attempts):
            last = self._read_send_button(sid, text)
            if last.get("ready"):
                return last
            time.sleep(0.4)
        return last

    def _click_send_button(self, sid):
        raw_clicked = self.cdp.eval_js(CLICK_SEND_BUTTON_JS, sid)
        try:
            return json.loads(raw_clicked) if isinstance(raw_clicked, str) else {}
        except (json.JSONDecodeError, ValueError, TypeError):
            return {"clicked": False, "reason": "invalid send click result"}

    def _send_message_in_session(self, sid, message):
        text = str(message or "").strip()
        if not text:
            return {"sent": False, "reason": "message empty"}

        raw_prepare = self.cdp.eval_js(PREPARE_MESSAGE_INPUT_JS, sid)
        try:
            prepared = json.loads(raw_prepare) if isinstance(raw_prepare, str) else {}
        except (json.JSONDecodeError, ValueError, TypeError):
            prepared = {"ready": False, "reason": "invalid input prepare result"}
        if not prepared.get("ready"):
            return {
                "sent": False,
                "reason": prepared.get("reason", "chat input not ready"),
                "debug": prepared,
            }

        inserted = self.cdp.send("Input.insertText", {"text": text}, sid)
        if inserted.get("error"):
            set_js = (
                "(function(){var text=__TEXT__;"
                "var el=Array.from(document.querySelectorAll('.chat-input[contenteditable], [contenteditable=true], [contenteditable=\"\"], textarea, input[type=text]'))"
                ".find(function(e){var r=e.getBoundingClientRect();return !!(r.width||r.height)&&!e.disabled&&!e.readOnly;});"
                "if(!el)return JSON.stringify({ready:false,reason:'chat input lost'});"
                "el.focus(); if('value' in el){el.value=text;}else{el.innerText=text;}"
                "el.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:text}));"
                "el.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true,key:'a'}));"
                "return JSON.stringify({ready:true});})()"
            ).replace("__TEXT__", json.dumps(text, ensure_ascii=False))
            raw_fallback = self.cdp.eval_js(set_js, sid)
            try:
                fallback = json.loads(raw_fallback) if isinstance(raw_fallback, str) else {}
            except (json.JSONDecodeError, ValueError, TypeError):
                fallback = {"ready": False, "reason": "invalid fallback input result"}
            if not fallback.get("ready"):
                return {
                    "sent": False,
                    "reason": fallback.get("reason", self._response_error(inserted)),
                    "debug": fallback,
                }

        time.sleep(0.2)
        send_info = self._read_send_button(sid, text)
        if not send_info.get("ready"):
            if not str(send_info.get("inputText") or "").strip():
                forced = self._force_set_message_input(sid, text)
                if forced.get("ready"):
                    send_info = self._wait_for_send_button(sid, text, timeout=4)
                elif forced:
                    send_info.setdefault("debug", {})
                    send_info["debug"]["forceInput"] = forced
            if send_info.get("ready"):
                pass
            elif str(send_info.get("inputText") or "").strip():
                self._press_enter(sid)
                time.sleep(1.2)
                verified = self._verify_message_sent(sid, text)
                if verified.get("sent"):
                    return {
                        "sent": True,
                        "verified": bool(verified.get("verified")),
                        "method": "enter",
                        "inputText": verified.get("inputText", ""),
                    }
            else:
                return {
                    "sent": False,
                    "reason": send_info.get("reason", "send button not ready"),
                    "debug": send_info,
                }

        clicked = self._click_send_button(sid)
        if not clicked.get("clicked"):
            if send_info.get("x") is None or send_info.get("y") is None:
                return {
                    "sent": False,
                    "reason": "send button coordinates missing",
                    "debug": {"send": send_info, "click": clicked},
                }
            self._click_point(sid, send_info.get("x"), send_info.get("y"))
        time.sleep(1.2)
        verified = self._verify_message_sent(sid, text)
        if verified.get("sent"):
            return {
                "sent": True,
                "verified": bool(verified.get("verified")),
                "method": "button",
                "inputText": verified.get("inputText", ""),
            }
        return {
            "sent": False,
            "reason": verified.get("reason", "message still not sent"),
            "debug": {
                "send": send_info,
                "click": clicked,
                "verify": verified,
            },
        }

    def fetch_current_chat(self, wait_for_messages=True):
        self.ensure_chat_target()
        deadline = time.time() + (CONVERSATION_OPEN_WAIT_SECONDS if wait_for_messages else 0)
        last_chat = {}
        while True:
            raw = self.cdp.eval_js(FETCH_CURRENT_CHAT_JS, self.chat_session_id)
            try:
                chat = json.loads(raw) if isinstance(raw, str) else {}
            except (json.JSONDecodeError, ValueError, TypeError):
                chat = {}
            last_chat = normalize_chat(chat) if isinstance(chat, dict) else {}
            if chat_message_count(last_chat) or not wait_for_messages or time.time() >= deadline:
                return last_chat
            time.sleep(CHAT_POLL_INTERVAL_SECONDS)

    def send_current_message(self, message):
        self.ensure_chat_target()
        return self._send_message_in_session(self.chat_session_id, message)

    def start_greeting(self, job, message=None):
        detail_url = boss.build_detail_url(job)
        if not detail_url:
            return {"sent": False, "reason": "job link missing"}
        target_id = self._create_target("about:blank", background=True)
        sid = self._attach_target(target_id)
        try:
            self.cdp.send("Page.navigate", {"url": detail_url}, sid)
            time.sleep(5)
            raw = self.cdp.eval_js(START_GREETING_JS, sid)
            try:
                result = json.loads(raw) if isinstance(raw, str) else {}
            except (json.JSONDecodeError, ValueError, TypeError):
                result = {"ready": False, "reason": "invalid greeting result"}
            if not result.get("ready"):
                return {"sent": False, "reason": result.get("reason", "start chat unavailable")}
            if result.get("error"):
                return {"sent": False, "reason": result.get("error", "start chat request failed"), "debug": result}
            if result.get("status") and int(result.get("status")) >= 400:
                return {"sent": False, "reason": f"start chat request HTTP {result.get('status')}", "debug": result}
            redirect = str(result.get("redirect") or "").strip()
            if redirect:
                redirect_url = f"https://www.zhipin.com{redirect}" if redirect.startswith("/") else redirect
                self.cdp.send("Page.navigate", {"url": redirect_url}, sid)
            time.sleep(5)
            message_result = {"sent": False, "reason": "no custom message"}
            if message:
                ready = self._wait_for_message_input(sid, timeout=8)
                if ready.get("ready"):
                    message_result = self._send_message_in_session(sid, message)
                else:
                    message_result = {
                        "sent": False,
                        "reason": ready.get("reason", "chat input not ready"),
                        "debug": ready,
                    }
            return {
                "sent": True,
                "button": result.get("text", ""),
                "job": job.get("title", ""),
                "message": str(message or ""),
                "custom_message_sent": bool(message_result.get("sent")),
                "custom_message_reason": message_result.get("reason", ""),
            }
        finally:
            self.cdp.send("Target.closeTarget", {"targetId": target_id}, timeout=5)


if work is not None:

    class BossLiveApp(App):
        CSS = """
        Screen {
            background: #0b1016;
            color: #dbe3ee;
        }

        Header {
            background: #121b24;
            color: #f4f7fb;
        }

        #topbar {
            height: 4;
            padding: 0 2;
            background: #0f171f;
            border-bottom: solid #263241;
        }

        #title_line {
            height: 1;
            text-style: bold;
            color: #f2c86b;
        }

        #status_line {
            height: 2;
            color: #8ea0b4;
        }

        #main {
            height: 1fr;
        }

        #left {
            width: 60%;
            min-width: 72;
            border-right: solid #263241;
        }

        #right {
            width: 40%;
            min-width: 44;
            padding: 0 2 1 2;
            background: #0b1016;
        }

        #job_table {
            height: 1fr;
            background: #0b1016;
            color: #dbe3ee;
        }

        #preview_title {
            height: auto;
            margin: 1 0 0 0;
            color: #f4f7fb;
            text-style: bold;
        }

        #preview_meta {
            height: auto;
            margin-bottom: 1;
            color: #8ea0b4;
        }

        #preview_scroll {
            height: 1fr;
            padding: 1 2;
            background: #0f171f;
            border: round #263241;
            overflow-y: auto;
        }

        #preview_body {
            height: auto;
            width: 1fr;
            padding-bottom: 1;
        }

        #reply_input {
            display: none;
            height: 3;
            margin-top: 1;
            border: round #334255;
            background: #0f171f;
            color: #dbe3ee;
        }

        .chat-ready #reply_input {
            display: block;
        }

        #greeting_modal {
            display: none;
            dock: bottom;
            height: 10;
            margin: 1 4;
            padding: 1 2;
            border: round #f2c86b;
            background: #0f171f;
            layer: modal;
        }

        .greeting #greeting_modal {
            display: block;
        }

        #greeting_title {
            height: 1;
            color: #f2c86b;
            text-style: bold;
        }

        #greeting_hint {
            height: 1;
            color: #8ea0b4;
        }

        #greeting_input {
            height: 3;
            margin-top: 1;
            border: round #334255;
            background: #0b1016;
            color: #dbe3ee;
        }

        #loading {
            height: 3;
            dock: bottom;
            display: none;
        }

        .loading #loading {
            display: block;
        }

        Footer {
            background: #0f171f;
            color: #8ea0b4;
        }
        """

        BINDINGS = [
            ("q", "quit", "退出"),
            ("escape", "back", "返回"),
            ("j", "show_jobs", "职位"),
            ("m", "show_messages", "消息"),
            ("n,right,pagedown", "next_page", "下一页"),
            ("p,left,pageup", "prev_page", "上一页"),
            ("r", "refresh", "刷新"),
            ("enter", "load_detail", "打开"),
            ("g", "greet", "打招呼"),
            ("o", "open_link", "打开网页"),
            ("d", "detail_down", "详情下滚"),
            ("u", "detail_up", "详情上滚"),
            ("tab", "toggle_focus", "切焦点"),
        ]

        def __init__(self, client, auto_detail=False):
            super().__init__()
            self.client = client
            self.auto_detail = auto_detail
            self.page = 1
            self.mode = "jobs"
            self.jobs = []
            self.jobs_by_id = {}
            self.selected_job_id = None
            self.detail = None
            self.detail_job_id = None
            self.loading_detail_job_id = None
            self.detail_request_seq = 0
            self.pending_greeting_job_id = None
            self.greeting_job = None
            self.conversations = []
            self.conversations_by_id = {}
            self.selected_conversation_id = None
            self.unread_total = 0
            self.chat = {}
            self.chat_conversation_id = None
            self.opening_conversation_id = None
            self.chat_request_seq = 0
            self.chat_operation_lock = threading.RLock()
            self.sending_conversation_id = None
            self.open_conversation_timer = None
            self.open_conversation_schedule_seq = 0
            self.suppress_table_highlight_until = 0
            self.is_loading = False
            self.status = "准备实时加载..."

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Vertical(id="topbar"):
                yield Label("", id="title_line")
                yield Label("", id="status_line")
            with Horizontal(id="main"):
                with Vertical(id="left"):
                    yield DataTable(id="job_table", zebra_stripes=True)
                with Vertical(id="right"):
                    yield Static("选择一个岗位", id="preview_title")
                    yield Static("", id="preview_meta")
                    with VerticalScroll(id="preview_scroll"):
                        yield Static("", id="preview_body")
                    yield Input(placeholder="输入回复，按 Enter 发送", id="reply_input")
            with Vertical(id="greeting_modal"):
                yield Static("", id="greeting_title")
                yield Static("", id="greeting_hint")
                yield Input(placeholder="编辑打招呼话术", id="greeting_input")
            yield LoadingIndicator(id="loading")
            yield Footer()

        def on_mount(self):
            self.title = "BOSS Live"
            self.sub_title = f"{self.client.keyword} @ {self.client.city_name}"
            table = self.query_one("#job_table", DataTable)
            table.cursor_type = "row"
            self.query_one("#preview_scroll", VerticalScroll).can_focus = True
            self.configure_job_table()
            self.refresh_header()
            table.focus()
            self.load_page(1)
            self.set_interval(60, self.poll_messages)

        def refresh_header(self):
            mode_label = "Jobs" if self.mode == "jobs" else f"Messages({self.unread_count()})"
            if self.mode == "messages":
                shortcuts = "Tab 切焦点 · Esc 职位 · 选中自动打开 · 输入框 Enter 发送 · r 刷新 · q 退出"
            else:
                shortcuts = "Tab 切焦点 · Enter 详情 · g 打招呼 · o 打开网页 · n/p 翻页 · m 消息 · r 刷新 · q 退出"
            self.query_one("#title_line", Label).update(
                f"BOSS Live · {self.client.keyword} @ {self.client.city_name} · {mode_label} · LIVE"
            )
            self.query_one("#status_line", Label).update(
                f"{format_filter_summary(self.client.filters)}  |  {self.status}\n{shortcuts}"
            )
            self.set_class(self.is_loading, "loading")
            self.set_class(self.mode == "messages", "messages")
            self.set_class(self.mode == "messages" and bool(self.chat.get("canReply")), "chat-ready")
            self.set_class(self.greeting_job is not None, "greeting")

        def unread_count(self):
            return self.unread_total or sum(1 for conv in self.conversations if conv.get("unread"))

        def set_status(self, message, loading=False):
            self.status = message
            self.is_loading = loading
            self.refresh_header()

        def focus_list_panel(self):
            self.query_one("#job_table", DataTable).focus()
            self.set_status("焦点已切到列表", False)

        def focus_detail_panel(self):
            if self.mode == "messages":
                reply_input = self.query_one("#reply_input", Input)
                if not reply_input.disabled:
                    reply_input.focus()
                    self.set_status("焦点已切到回复输入框", False)
                    return
            self.query_one("#preview_scroll", VerticalScroll).focus()
            self.set_status("焦点已切到详情面板", False)

        def action_toggle_focus(self):
            focused = self.focused
            if focused is self.query_one("#job_table", DataTable):
                self.focus_detail_panel()
            else:
                self.focus_list_panel()

        def configure_job_table(self):
            table = self.query_one("#job_table", DataTable)
            table.clear(columns=True)
            table.add_columns("#", "职位", "薪资", "公司", "地点/要求", "技能")

        def configure_message_table(self):
            table = self.query_one("#job_table", DataTable)
            table.clear(columns=True)
            table.add_columns("#", "联系人/公司", "最近消息", "状态")

        def action_show_jobs(self):
            self.mode = "jobs"
            self.chat = {}
            self.chat_conversation_id = None
            self.opening_conversation_id = None
            self.chat_request_seq += 1
            self.selected_conversation_id = None
            self.configure_job_table()
            self.apply_page(self.page, self.jobs)

        def action_back(self):
            if self.greeting_job:
                self.cancel_greeting()
                return
            if self.mode == "messages":
                self.action_show_jobs()
            else:
                self.set_status("已经在职位列表")

        def action_show_messages(self):
            self.mode = "messages"
            self.chat = {}
            self.chat_conversation_id = None
            self.opening_conversation_id = None
            self.chat_request_seq += 1
            self.configure_message_table()
            self.load_conversations()

        def action_next_page(self):
            if self.mode == "messages":
                self.load_conversations()
                return
            self.load_page(self.page + 1)

        def action_prev_page(self):
            if self.mode == "messages":
                self.set_status("消息模式不分页，按 r 刷新")
                return
            if self.page > 1:
                self.load_page(self.page - 1)
            else:
                self.set_status("已经是第一页")

        def action_refresh(self):
            if self.mode == "messages":
                self.load_conversations()
            else:
                self.load_page(self.page)

        def current_job_link(self):
            if self.mode != "jobs":
                return ""
            job = self.current_job()
            return str((job or {}).get("job_link") or "").strip()

        def action_open_link(self):
            url = self.current_job_link()
            if not url:
                self.set_status("当前没有可打开的岗位链接")
                return
            if webbrowser.open(url):
                self.set_status(f"已打开网页: {short_link_label(url)}")
            else:
                self.set_status("打开网页失败")

        def action_greet(self):
            if self.greeting_job:
                self.confirm_greeting()
                return
            if self.mode != "jobs":
                self.set_status("请先按 j 回到职位列表，再选择岗位打招呼")
                return
            job = self.current_job()
            if not job:
                return
            self.show_greeting_modal(job)

        def show_greeting_modal(self, job):
            self.greeting_job = job
            self.pending_greeting_job_id = job.get("job_id")
            self.query_one("#greeting_title", Static).update(f"打招呼草稿 · {job.get('title', '')}")
            self.query_one("#greeting_hint", Static).update("可编辑。Esc 取消；不编辑时再次按 g 确认；输入框内按 Enter 也会确认。")
            greeting_input = self.query_one("#greeting_input", Input)
            greeting_input.value = default_greeting_message(job)
            greeting_input.focus()
            self.set_status("打招呼草稿已准备，确认前不会发送")
            self.refresh_header()

        def cancel_greeting(self):
            self.greeting_job = None
            self.pending_greeting_job_id = None
            self.query_one("#greeting_input", Input).value = ""
            self.set_status("已取消打招呼")

        def confirm_greeting(self):
            job = self.greeting_job
            if not job:
                return
            message = self.query_one("#greeting_input", Input).value.strip()
            self.greeting_job = None
            self.pending_greeting_job_id = None
            self.refresh_header()
            if not message:
                self.set_status("打招呼内容为空，已取消")
                return
            self.start_greeting(job, message)

        def action_load_detail(self):
            if self.mode == "messages":
                conv = self.current_conversation()
                if conv:
                    self.open_selected_conversation_if_needed(force=True)
            else:
                job = self.current_job()
                if job:
                    self.request_detail(job, force=True)

        def action_detail_down(self):
            self.query_one("#preview_scroll", VerticalScroll).scroll_page_down()

        def action_detail_up(self):
            self.query_one("#preview_scroll", VerticalScroll).scroll_page_up()

        def current_job(self):
            if self.selected_job_id:
                return self.jobs_by_id.get(self.selected_job_id)
            return self.jobs[0] if self.jobs else None

        def current_conversation(self):
            if self.selected_conversation_id:
                return self.conversations_by_id.get(self.selected_conversation_id)
            return self.conversations[0] if self.conversations else None

        def use_cached_detail_if_available(self, job):
            cached = self.client.get_cached_detail(job)
            if not cached:
                return False
            self.detail = cached
            self.detail_job_id = job.get("job_id")
            self.loading_detail_job_id = None
            jd_len = len((cached or {}).get("jd", ""))
            self.set_status(f"详情缓存命中: JD {jd_len} 字", False)
            return True

        def request_detail(self, job, force=False):
            job_id = job.get("job_id")
            if self.detail_job_id == job_id and self.detail:
                self.update_preview()
                return
            if self.use_cached_detail_if_available(job):
                self.update_preview()
                return
            if self.loading_detail_job_id == job_id:
                return
            self.detail = None
            self.detail_job_id = None
            self.loading_detail_job_id = job_id
            self.detail_request_seq += 1
            request_seq = self.detail_request_seq
            self.update_preview()
            self.load_detail(job, request_seq, force)

        def on_data_table_row_highlighted(self, event):
            if time.time() < self.suppress_table_highlight_until:
                return
            self.select_row_key(event.row_key)

        def on_data_table_row_selected(self, event):
            self.select_row_key(event.row_key)
            if self.mode == "messages":
                self.open_selected_conversation_if_needed(force=True)
            else:
                job = self.current_job()
                if job:
                    self.request_detail(job, force=True)

        def select_row_key(self, row_key):
            key = getattr(row_key, "value", str(row_key))
            if self.mode == "messages" and key in self.conversations_by_id:
                previous_key = self.selected_conversation_id
                self.selected_conversation_id = key
                if previous_key != key:
                    self.chat = {}
                    self.chat_conversation_id = None
                    self.opening_conversation_id = None
                self.open_selected_conversation_if_needed()
            elif key in self.jobs_by_id:
                self.selected_job_id = key
                job = self.jobs_by_id[key]
                if self.auto_detail:
                    self.request_detail(job)
                elif self.use_cached_detail_if_available(job):
                    self.update_preview()
                else:
                    self.update_preview()

        @work(thread=True)
        def load_page(self, page):
            self.call_from_thread(self.set_status, f"正在实时加载第 {page} 页...", True)
            try:
                jobs = self.client.fetch_page(page)
            except Exception as exc:
                self.call_from_thread(self.apply_page_error, page, exc)
                return
            self.call_from_thread(self.apply_page, page, jobs)

        def apply_page_error(self, page, exc):
            self.set_status(f"第 {page} 页加载失败: {exc}", False)

        def apply_page(self, page, jobs):
            if not jobs and page > 1:
                self.set_status(f"第 {page} 页没有返回岗位", False)
                return
            self.page = page
            self.jobs = jobs
            self.jobs_by_id = {job.get("job_id", ""): job for job in jobs if job.get("job_id")}
            self.selected_job_id = jobs[0].get("job_id") if jobs else None
            self.detail = None
            self.detail_job_id = None
            self.loading_detail_job_id = None
            self.detail_request_seq += 1
            self.pending_greeting_job_id = None

            table = self.query_one("#job_table", DataTable)
            if self.mode != "jobs":
                return
            self.suppress_table_highlight_until = time.time() + 0.1
            self.configure_job_table()
            for index, job in enumerate(jobs, start=1):
                key = job.get("job_id") or str(index)
                table.add_row(
                    f"{index:02d}",
                    short_text(job.get("title"), 30),
                    short_text(job.get("salary"), 13),
                    short_text(job.get("boss_name"), 18),
                    short_text(f"{boss.district_from_location(job.get('location'))} · {job.get('tags', '')}", 22),
                    short_text(first_nonempty(job.get("skills"), job.get("job_labels")), 26),
                    key=key,
                )

            self.set_status(f"第 {page} 页实时加载完成: {len(jobs)} 条", False)
            if jobs:
                if self.auto_detail:
                    self.request_detail(jobs[0])
                elif self.use_cached_detail_if_available(jobs[0]):
                    self.update_preview()
                else:
                    self.update_preview()
            else:
                self.update_preview()

        @work(thread=True)
        def load_conversations(self):
            self.call_from_thread(self.set_status, "正在读取 BOSS 消息列表...", True)
            try:
                conversations = self.client.fetch_conversations()
            except Exception as exc:
                self.call_from_thread(self.apply_conversations_error, exc)
                return
            self.call_from_thread(self.apply_conversations, conversations)

        def apply_conversations_error(self, exc):
            self.set_status(f"消息列表加载失败: {exc}", False)

        def apply_conversations(self, conversations):
            if not conversations and self.conversations:
                self.set_status("本次消息刷新为空，已保留上一份会话列表", False)
                self.update_chat_preview()
                return
            previous_selected_id = self.selected_conversation_id
            self.mode = "messages"
            self.conversations = conversations
            self.unread_total = sum(1 for conv in conversations if conv.get("unread"))
            self.conversations_by_id = {
                conv.get("conversation_id", ""): conv
                for conv in conversations
                if conv.get("conversation_id")
            }
            if previous_selected_id in self.conversations_by_id:
                self.selected_conversation_id = previous_selected_id
            else:
                self.selected_conversation_id = conversations[0].get("conversation_id") if conversations else None
                self.chat = {}
                self.chat_conversation_id = None
            table = self.query_one("#job_table", DataTable)
            self.suppress_table_highlight_until = time.time() + 0.1
            self.configure_message_table()
            for index, conv in enumerate(conversations, start=1):
                key = conv.get("conversation_id") or str(index)
                status = conv.get("status_label") or conversation_status_label(conv)
                table.add_row(
                    f"{index:02d}",
                    short_text(conv.get("title"), 30),
                    short_text(conv.get("preview"), 58),
                    status,
                    key=key,
                )
            self.set_status(f"消息列表已实时刷新: {len(conversations)} 条，会话未读 {self.unread_count()} 条", False)
            if conversations:
                self.open_selected_conversation_if_needed()
            else:
                self.update_chat_preview()

        def open_selected_conversation_if_needed(self, force=False):
            conv = self.current_conversation()
            if not conv:
                return False
            conv_id = conv.get("conversation_id")
            if not force:
                if conv_id and self.chat_conversation_id == conv_id and self.chat:
                    return True
                if conv_id and self.opening_conversation_id == conv_id:
                    return True
                self.schedule_open_conversation(conv)
                return True
            self.cancel_open_conversation_timer()
            return self.start_open_conversation(conv)

        def cancel_open_conversation_timer(self, invalidate=True):
            if invalidate:
                self.open_conversation_schedule_seq += 1
            if self.open_conversation_timer:
                self.open_conversation_timer.stop()
                self.open_conversation_timer = None

        def schedule_open_conversation(self, conversation):
            self.cancel_open_conversation_timer()
            conv = dict(conversation or {})
            conv_id = conv.get("conversation_id")
            self.open_conversation_schedule_seq += 1
            schedule_seq = self.open_conversation_schedule_seq
            self.opening_conversation_id = conv_id
            self.chat = {}
            self.chat_conversation_id = None
            self.update_chat_preview()
            self.open_conversation_timer = self.set_timer(
                CONVERSATION_OPEN_DEBOUNCE_SECONDS,
                lambda: self.start_open_conversation(conv, schedule_seq=schedule_seq),
                name="open-conversation-debounce",
            )

        def start_open_conversation(self, conversation, schedule_seq=None):
            if schedule_seq is not None and schedule_seq != self.open_conversation_schedule_seq:
                return False
            self.cancel_open_conversation_timer(invalidate=False)
            conv_id = conversation.get("conversation_id")
            if conv_id and conv_id != self.selected_conversation_id:
                return False
            self.opening_conversation_id = conv_id
            same_visible_chat = conv_id and self.chat_conversation_id == conv_id and self.chat
            if not same_visible_chat:
                self.chat = {}
                self.chat_conversation_id = None
            self.chat_request_seq += 1
            request_seq = self.chat_request_seq
            self.update_chat_preview()
            self.open_conversation(conversation, request_seq)
            return True

        @work(thread=True)
        def poll_messages(self):
            if self.is_loading:
                return
            try:
                conversations = self.client.fetch_conversations()
            except Exception:
                return
            self.call_from_thread(self.apply_message_poll, conversations)

        def apply_message_poll(self, conversations):
            if not conversations:
                if self.mode == "messages" and self.conversations:
                    self.set_status("本次消息轮询为空，已保留上一份会话列表", False)
                else:
                    self.refresh_header()
                return
            self.unread_total = sum(1 for conv in conversations if conv.get("unread"))
            if self.mode == "messages":
                self.apply_conversations(conversations)
                return
            if self.unread_total:
                self.conversations = conversations
                self.conversations_by_id = {
                    conv.get("conversation_id", ""): conv
                    for conv in conversations
                    if conv.get("conversation_id")
                }
                self.set_status(f"有 {self.unread_total} 条未读消息，按 m 查看", False)
            else:
                self.refresh_header()

        @work(thread=True)
        def open_conversation(self, conversation, request_seq):
            self.call_from_thread(self.set_status, f"正在打开会话: {conversation.get('title', '')}", True)
            try:
                with self.chat_operation_lock:
                    self.client.open_conversation(conversation.get("index", 0))
                    chat = self.client.fetch_current_chat()
            except Exception as exc:
                self.call_from_thread(self.apply_chat_error, exc, request_seq)
                return
            self.call_from_thread(self.apply_chat, conversation, chat, request_seq)

        def apply_chat_error(self, exc, request_seq=None):
            if request_seq is not None and request_seq != self.chat_request_seq:
                return
            self.opening_conversation_id = None
            self.set_status(f"会话加载失败: {exc}", False)

        def apply_chat(self, conversation, chat, request_seq=None):
            conv_id = conversation.get("conversation_id")
            if request_seq is not None and request_seq != self.chat_request_seq:
                return
            if conv_id and conv_id != self.selected_conversation_id:
                return
            self.selected_conversation_id = conv_id
            self.chat_conversation_id = conv_id
            self.opening_conversation_id = None
            self.chat = merge_chat_without_losing_history(self.chat, chat)
            if self.chat.get("_preserved_messages"):
                self.set_status(f"会话实时刷新为空，已保留当前历史: {conversation.get('title', '')}", False)
            else:
                self.set_status(f"会话已实时打开: {conversation.get('title', '')}", False)
            self.update_chat_preview()

        def update_chat_preview(self):
            reply_input = self.query_one("#reply_input", Input)
            conv_for_input = self.current_conversation()
            conv_for_input_id = conv_for_input.get("conversation_id") if conv_for_input else None
            reply_input.disabled = (
                not bool(self.chat.get("canReply"))
                or self.chat_conversation_id != conv_for_input_id
                or self.sending_conversation_id == conv_for_input_id
            )
            if reply_input.disabled:
                reply_input.value = ""
            conv = self.current_conversation()
            if not conv:
                self.query_one("#preview_title", Static).update("没有消息")
                self.query_one("#preview_meta", Static).update("")
                self.query_one("#preview_body", Static).update("按 m 刷新消息列表。")
                self.refresh_header()
                return
            conv_id = conv.get("conversation_id")
            self.query_one("#preview_title", Static).update(conv.get("title", ""))
            status = conv.get("status_label") or conversation_status_label(conv)
            meta = short_text(conv.get("preview"), 120)
            if status:
                meta = f"{status} · {meta}"
            self.query_one("#preview_meta", Static).update(meta)
            if self.sending_conversation_id == conv_id:
                self.query_one("#preview_body", Static).update(
                    f"[bold {COLOR_ACCENT}]会话[/]\n{chat_hint('正在发送回复')}\n\n[{COLOR_FAINT}]•  •  •[/]"
                )
            elif self.chat and self.chat_conversation_id == conv_id:
                self.query_one("#preview_body", Static).update(format_chat_text(conv, self.chat))
            elif self.opening_conversation_id == conv_id:
                self.query_one("#preview_body", Static).update(
                    f"[bold {COLOR_ACCENT}]会话[/]\n{chat_hint('正在加载聊天记录')}\n\n[{COLOR_FAINT}]•  •  •[/]"
                )
            else:
                self.query_one("#preview_body", Static).update(
                    f"[bold {COLOR_ACCENT}]会话[/]\n{chat_hint('聊天记录会自动加载。')}\n\n"
                    f"[{COLOR_HINT_DIM}]{chat_body_text(conv.get('text', ''))}[/]"
                )
            self.query_one("#preview_scroll", VerticalScroll).scroll_home(animate=False)
            self.refresh_header()

        def on_input_submitted(self, event):
            if event.input.id == "greeting_input":
                self.confirm_greeting()
                return
            if event.input.id != "reply_input" or self.mode != "messages":
                return
            message = event.value.strip()
            if not message:
                return
            conv = self.current_conversation()
            conv_id = conv.get("conversation_id") if conv else None
            if not conv or not self.chat.get("canReply") or self.chat_conversation_id != conv_id:
                event.input.value = ""
                self.set_status("等待会话加载完成后再输入回复", False)
                return
            event.input.value = ""
            self.sending_conversation_id = conv_id
            self.update_chat_preview()
            self.send_message(message, dict(conv), self.chat_request_seq)

        @work(thread=True)
        def send_message(self, message, conversation, request_seq):
            conv_id = (conversation or {}).get("conversation_id")
            if not conv_id:
                self.call_from_thread(self.set_status, "发送失败: 未找到当前会话", False)
                return
            self.call_from_thread(self.set_status, f"正在发送回复: {conversation.get('title', '')}", True)
            try:
                with self.chat_operation_lock:
                    self.client.open_conversation(conversation.get("index", 0))
                    result = self.client.send_current_message(message)
                    chat = self.client.fetch_current_chat()
            except Exception as exc:
                self.call_from_thread(self.apply_send_error, exc, conv_id)
                return
            self.call_from_thread(self.apply_send_result, result, chat, message, conversation, request_seq)

        def apply_send_error(self, exc, conv_id=None):
            if conv_id and self.sending_conversation_id == conv_id:
                self.sending_conversation_id = None
            self.set_status(f"发送失败: {short_text(exc, 80)}", False)

        def refresh_message_table(self):
            table = self.query_one("#job_table", DataTable)
            self.configure_message_table()
            for index, conv in enumerate(self.conversations, start=1):
                key = conv.get("conversation_id") or str(index)
                status = conv.get("status_label") or conversation_status_label(conv)
                table.add_row(
                    f"{index:02d}",
                    short_text(conv.get("title"), 30),
                    short_text(conv.get("preview"), 58),
                    status,
                    key=key,
                )

        def apply_send_result(self, result, chat, message, conversation=None, request_seq=None):
            conversation = conversation or {}
            sent_conv_id = conversation.get("conversation_id")
            if sent_conv_id and self.sending_conversation_id == sent_conv_id:
                self.sending_conversation_id = None
            if result.get("sent"):
                fallback_chat = self.chat if self.chat_conversation_id == sent_conv_id else {}
                updated_chat = append_local_message(merge_chat_without_losing_history(fallback_chat, chat), message)
                conv = self.conversations_by_id.get(sent_conv_id) or conversation
                if conv:
                    update_conversation_preview(conv, message)
                    conv_id = conv.get("conversation_id")
                    if conv_id:
                        self.conversations_by_id[conv_id] = conv
                    self.unread_total = sum(1 for item in self.conversations if item.get("unread"))
                    if self.mode == "messages":
                        self.refresh_message_table()
                if sent_conv_id and sent_conv_id == self.selected_conversation_id:
                    self.chat = updated_chat
                    self.chat_conversation_id = sent_conv_id
                    self.set_status("回复已发送", False)
                    self.update_chat_preview()
                else:
                    self.set_status(f"回复已发送: {conversation.get('title', '')}", False)
            else:
                self.set_status(f"发送失败: {short_text(format_send_failure(result), 80)}", False)
                self.update_chat_preview()

        @work(thread=True)
        def start_greeting(self, job, message):
            self.call_from_thread(self.set_status, f"正在打开岗位并执行打招呼: {job.get('title', '')}", True)
            try:
                result = self.client.start_greeting(job, message)
                conversations = self.client.fetch_conversations()
            except Exception as exc:
                self.call_from_thread(self.apply_greeting_error, exc)
                return
            self.call_from_thread(self.apply_greeting_result, result, conversations)

        def apply_greeting_error(self, exc):
            self.set_status(f"打招呼失败: {exc}", False)

        def apply_greeting_result(self, result, conversations):
            self.conversations = conversations
            self.conversations_by_id = {
                conv.get("conversation_id", ""): conv
                for conv in conversations
                if conv.get("conversation_id")
            }
            self.unread_total = sum(1 for conv in conversations if conv.get("unread"))
            if result.get("sent"):
                if result.get("custom_message_sent"):
                    self.set_status("已打招呼并发送自定义话术；按 m 查看消息列表", False)
                else:
                    reason = result.get("custom_message_reason") or "未找到可发送输入框"
                    self.set_status(f"已点击立即沟通；自定义话术未确认发送: {reason}", False)
            else:
                self.set_status(f"打招呼失败: {result.get('reason', '未知原因')}", False)

        @work(thread=True)
        def load_detail(self, job, request_seq=None, force=False):
            title = job.get("title", "")
            job_id = job.get("job_id")
            if not force:
                time.sleep(DETAIL_AUTOLOAD_DELAY_SECONDS)
                if request_seq != self.detail_request_seq or self.selected_job_id != job_id:
                    return
            self.call_from_thread(self.set_status, f"正在实时拉取详情: {title}", True)
            try:
                detail = self.client.fetch_detail(job)
            except Exception as exc:
                self.call_from_thread(self.apply_detail_error, job, exc)
                return
            self.call_from_thread(self.apply_detail, job, detail)

        def apply_detail_error(self, job, exc):
            job_id = job.get("job_id")
            if self.loading_detail_job_id == job_id:
                self.loading_detail_job_id = None
            if self.selected_job_id == job_id:
                self.set_status(f"详情加载失败: {exc}", False)
                self.update_preview()

        def apply_detail(self, job, detail):
            job_id = job.get("job_id")
            if self.loading_detail_job_id == job_id:
                self.loading_detail_job_id = None
            if self.selected_job_id != job_id:
                self.refresh_header()
                return
            self.detail = detail
            self.detail_job_id = job_id
            jd_len = len((detail or {}).get("jd", ""))
            if (detail or {}).get("_cache_hit"):
                self.set_status(f"详情缓存命中: JD {jd_len} 字", False)
            else:
                self.set_status(f"详情已实时更新: JD {jd_len} 字", False)
            self.update_preview()

        def update_preview(self):
            job = self.current_job()
            if not job:
                self.query_one("#preview_title", Static).update("没有岗位数据")
                self.query_one("#preview_meta", Static).update("")
                self.query_one("#preview_body", Static).update("当前页没有返回职位。")
                return

            self.query_one("#preview_title", Static).update(job.get("title", ""))
            self.query_one("#preview_meta", Static).update(
                " · ".join(
                    item for item in (
                        first_nonempty(job.get("salary"), "-"),
                        first_nonempty(job.get("boss_name"), "-"),
                        first_nonempty(job.get("location"), "-"),
                        first_nonempty(job.get("tags"), "-"),
                    )
                    if item and item != "-"
                )
            )

            detail = self.detail if self.detail_job_id == job.get("job_id") else None
            loading = self.loading_detail_job_id == job.get("job_id")
            self.query_one("#preview_body", Static).update(
                format_preview_text(job, detail, auto_detail=self.auto_detail, loading=loading)
            )
            self.query_one("#preview_scroll", VerticalScroll).scroll_home(animate=False)


def build_arg_parser():
    parser = argparse.ArgumentParser(description="实时终端浏览 BOSS 直聘职位（只走 Chrome CDP，不读离线 JSON）。")
    parser.add_argument("--keyword", default="AI Agent", help="搜索关键词")
    parser.add_argument("--city", default=boss.DEFAULT_CITY_INPUT, help="城市（中文名或代码）")
    parser.add_argument("--cdp-port", type=int, default=boss.DEFAULT_CDP_PORT, help="CDP 调试端口")
    parser.add_argument("--scale", default=None, help="公司规模代码")
    parser.add_argument("--stage", default=None, help="融资阶段代码")
    parser.add_argument("--salary", default=None, help="薪资范围代码")
    parser.add_argument("--experience", default=None, help="经验要求代码")
    parser.add_argument("--degree", default=None, help="学历要求代码")
    parser.add_argument("--industry", default=None, help="行业代码")
    parser.add_argument("--auto-detail", action="store_true", help="选中岗位后自动加载详情（默认关闭，会增加详情请求频率）")
    return parser


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not require_textual():
        return 1
    if not boss.require_runtime_dependencies("requests", "websocket"):
        return 1
    if not boss.check_login_state(args.cdp_port):
        print("未检测到 BOSS 登录态。请先运行: python3 scripts/boss_cdp_raw.py --setup-chrome")
        return 1

    client = None
    try:
        client = LiveBossClient(args.keyword, args.city, build_filters(args), cdp_port=args.cdp_port)
        BossLiveApp(client, auto_detail=args.auto_detail).run()
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
