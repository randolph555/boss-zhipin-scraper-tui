#!/usr/bin/env python3
"""Probe BOSS Zhipin message/chat page structure via CDP without sending messages."""

import argparse
import json
import sys
import time

try:
    from scripts import boss_cdp_raw as boss
except ImportError:
    import boss_cdp_raw as boss


DEFAULT_CANDIDATE_URLS = [
    "https://www.zhipin.com/web/geek/chat",
    "https://www.zhipin.com/web/geek/message",
    "https://www.zhipin.com/web/geek/index",
]


PROBE_JS = r"""
(function(){
  function txt(el, limit) {
    return (el.innerText || el.textContent || el.value || '')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, limit || 160);
  }
  function attr(el, name) {
    return el.getAttribute(name) || '';
  }
  function item(el) {
    var rect = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      text: txt(el, 180),
      href: el.href || attr(el, 'href'),
      id: el.id || '',
      class: el.className || '',
      role: attr(el, 'role'),
      aria: attr(el, 'aria-label'),
      placeholder: attr(el, 'placeholder'),
      contenteditable: attr(el, 'contenteditable'),
      visible: !!(rect.width || rect.height),
      rect: [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)]
    };
  }
  var clickable = Array.from(document.querySelectorAll(
    'a,button,[role=button],input,textarea,[contenteditable=true],[contenteditable=""],.chat-list li,.conversation-list li'
  )).slice(0, 120).map(item);
  var likelyConversations = Array.from(document.querySelectorAll(
    '[class*="chat"],[class*="conversation"],[class*="message"],[class*="dialog"],[class*="item"],li'
  )).filter(function(el){
    var t = txt(el, 240);
    return t && t.length >= 4 && /(聊|沟通|消息|未读|新消息|职位|公司|HR|Boss|BOSS|刚刚|\d{1,2}:\d{2})/.test(t);
  }).slice(0, 80).map(item);
  var inputs = Array.from(document.querySelectorAll(
    'textarea,input,[contenteditable=true],[contenteditable=""]'
  )).slice(0, 40).map(item);
  return JSON.stringify({
    url: location.href,
    title: document.title,
    body: txt(document.body, 4000),
    clickable: clickable,
    likelyConversations: likelyConversations,
    inputs: inputs,
    localStorageKeys: Object.keys(localStorage || {}).slice(0, 80),
    sessionStorageKeys: Object.keys(sessionStorage || {}).slice(0, 80)
  });
})()
"""

CLICK_FIRST_CONVERSATION_JS = r"""
(function(){
  var items = Array.from(document.querySelectorAll('.chat-content li'))
    .filter(function(el){
      var rect = el.getBoundingClientRect();
      return !!(rect.width || rect.height);
    });
  if (!items.length) return JSON.stringify({clicked: false, reason: 'no visible .chat-content li'});
  var rect = items[0].getBoundingClientRect();
  return JSON.stringify({
    clicked: false,
    text: (items[0].innerText || '').replace(/\s+/g, ' ').trim(),
    x: Math.round(rect.left + Math.min(40, rect.width / 2)),
    y: Math.round(rect.top + Math.min(28, rect.height / 2))
  });
})()
"""


def probe_url(cdp, url, wait_seconds, click_first=False):
    r = cdp.send("Target.createTarget", {"url": "about:blank"})
    target_id = r["result"]["targetId"]
    r = cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
    session_id = r["result"]["sessionId"]
    try:
        cdp.send("Page.navigate", {"url": url}, session_id)
        time.sleep(wait_seconds)
        clicked = None
        if click_first:
            raw_clicked = cdp.eval_js(CLICK_FIRST_CONVERSATION_JS, session_id)
            try:
                clicked = json.loads(raw_clicked) if isinstance(raw_clicked, str) else raw_clicked
            except (json.JSONDecodeError, ValueError, TypeError):
                clicked = {"clicked": False, "reason": "invalid click result"}
            if clicked.get("x") is not None and clicked.get("y") is not None:
                x = clicked["x"]
                y = clicked["y"]
                cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, session_id)
                cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}, session_id)
                cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}, session_id)
                clicked["clicked"] = True
            time.sleep(wait_seconds)
        raw = cdp.eval_js(PROBE_JS, session_id)
        data = json.loads(raw) if isinstance(raw, str) else {}
        if clicked is not None:
            data["clickedConversation"] = clicked
        return data
    finally:
        cdp.send("Target.closeTarget", {"targetId": target_id}, timeout=5)


def compact_elements(elements, max_items):
    rows = []
    for item in elements[:max_items]:
        rows.append({
            "tag": item.get("tag", ""),
            "text": item.get("text", ""),
            "href": item.get("href", ""),
            "class": item.get("class", ""),
            "placeholder": item.get("placeholder", ""),
            "contenteditable": item.get("contenteditable", ""),
            "visible": item.get("visible", False),
            "rect": item.get("rect", []),
        })
    return rows


def print_probe(data, max_items):
    print("=" * 80)
    print(f"URL: {data.get('url', '')}")
    print(f"TITLE: {data.get('title', '')}")
    print("- BODY -")
    print(data.get("body", "")[:2000])
    if "clickedConversation" in data:
        print("- CLICKED CONVERSATION -")
        print(json.dumps(data.get("clickedConversation", {}), ensure_ascii=False, indent=2))
    print("- INPUTS -")
    print(json.dumps(compact_elements(data.get("inputs", []), max_items), ensure_ascii=False, indent=2))
    print("- CLICKABLE -")
    print(json.dumps(compact_elements(data.get("clickable", []), max_items), ensure_ascii=False, indent=2))
    print("- LIKELY CONVERSATIONS -")
    print(json.dumps(compact_elements(data.get("likelyConversations", []), max_items), ensure_ascii=False, indent=2))
    print("- STORAGE KEYS -")
    print(json.dumps({
        "localStorage": data.get("localStorageKeys", []),
        "sessionStorage": data.get("sessionStorageKeys", []),
    }, ensure_ascii=False, indent=2))


def build_arg_parser():
    parser = argparse.ArgumentParser(description="只读探测 BOSS 消息/聊天页 DOM 结构，不发送消息。")
    parser.add_argument("--cdp-port", type=int, default=boss.DEFAULT_CDP_PORT)
    parser.add_argument("--url", action="append", help="指定要探测的消息页 URL；可传多次")
    parser.add_argument("--wait", type=float, default=6.0, help="每个页面加载等待秒数")
    parser.add_argument("--max-items", type=int, default=20, help="每类元素最多打印数量")
    parser.add_argument("--click-first", action="store_true", help="点击第一条可见会话后再探测（只读，不发送）")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    if not boss.require_runtime_dependencies("requests", "websocket"):
        return 1
    if not boss.check_login_state(args.cdp_port):
        print("未检测到 BOSS 登录态。请先运行: python3 scripts/boss_cdp_raw.py --setup-chrome")
        return 1

    cdp = boss.CDPSession(args.cdp_port)
    try:
        for url in args.url or DEFAULT_CANDIDATE_URLS:
            try:
                print_probe(probe_url(cdp, url, args.wait, click_first=args.click_first), args.max_items)
            except Exception as exc:
                print(f"探测失败 {url}: {exc}")
    finally:
        cdp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
