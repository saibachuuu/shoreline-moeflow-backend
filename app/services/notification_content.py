"""Small bounded BBCode parser shared by preview, storage, web nodes and email rendering."""

import html
import re
import unicodedata
from urllib.parse import urlsplit
from bson import ObjectId
from mongoengine import DoesNotExist
from app.core.api import APIError


class NotificationError(APIError):
    code = 4600

    def __init__(self, message, status=400):
        super().__init__(message, replace=True)
        self.status_code = status


def fail(message, status=400):
    raise NotificationError(message, status)


def safe_url(value):
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(ord(c) < 32 for c in value)
    ):
        return False
    try:
        parsed = urlsplit(value.strip())
        return (
            parsed.scheme.lower() in ("http", "https")
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def parse_body(text):
    if not isinstance(text, str) or len(text) > 10000:
        fail("通知正文不能超过 10000 字符")
    root = []
    stack = [("root", root)]
    tokens = re.split(
        r"(\[(?:/?(?:b|i|u|s|quote|list|url|project)|\*)(?:=[^\]\r\n]*)?\])",
        text,
        flags=re.I,
    )
    count = 0
    for token in tokens:
        if not token:
            continue
        count += 1
        if count > 2000:
            fail("通知格式节点过多")
        match = re.fullmatch(
            r"\[(/?)(b|i|u|s|quote|list|url|project|\*)(?:=([^\]]*))?\]", token, re.I
        )
        if not match:
            stack[-1][1].append({"type": "text", "text": token})
            continue
        closing, tag, arg = match.groups()
        tag = tag.lower()
        if tag == "*":
            if stack[-1][0] == "li":
                stack.pop()
            if stack[-1][0] != "list":
                fail("列表项必须位于 list 内")
            node = {"type": "li", "children": []}
            stack[-1][1].append(node)
            stack.append(("li", node["children"]))
        elif closing:
            if tag == "list" and stack[-1][0] == "li":
                stack.pop()
            if len(stack) == 1 or stack[-1][0] != tag:
                fail("BBCode 标签未正确闭合")
            stack.pop()
        else:
            if len(stack) >= 12:
                fail("BBCode 嵌套过深")
            if arg and tag != "url":
                fail("不支持的标签参数")
            node = {"type": tag, "children": []}
            if tag == "url" and arg:
                if not safe_url(arg):
                    fail("链接只支持安全的 HTTP/HTTPS 地址")
                node["url"] = arg.strip()
            stack[-1][1].append(node)
            stack.append((tag, node["children"]))
    if len(stack) != 1:
        fail("BBCode 标签未正确闭合")
    return root


def plain(nodes):
    return "".join(
        n.get("text", "") if n["type"] == "text" else plain(n.get("children", []))
        for n in nodes
    )


def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(node.get("children", []))


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def search_tokens(text):
    value = normalize(text)
    return sorted({value[i : i + 2] for i in range(len(value) - 1)})


def project_accessible(project, user):
    try:
        return bool(
            project and user and not user.banned and user.can(project, "project:ACCESS")
        )
    except DoesNotExist:
        return False


def prepare(text, actor):
    from flask import current_app
    from app.models.project import Project

    nodes = parse_body(text)
    projects = set()
    for node in walk(nodes):
        if node["type"] == "url":
            url = node.get("url") or plain(node["children"])
            if not safe_url(url):
                fail("链接只支持安全的 HTTP/HTTPS 地址")
            node["url"] = url.strip()
        if node["type"] == "project":
            value = plain(node["children"]).strip()
            if not ObjectId.is_valid(value):
                parsed = urlsplit(value)
                site = urlsplit(current_app.config.get("SITE_ORIGIN", ""))
                if (
                    parsed.scheme not in ("http", "https")
                    or parsed.netloc != site.netloc
                ):
                    fail("项目名片只接受本站项目链接或 ID")
                match = re.search(
                    r"/(?:projects?|project)/([a-fA-F0-9]{24})(?:/|$)", parsed.path
                )
                value = match.group(1) if match else ""
            if not ObjectId.is_valid(value):
                fail("项目名片无效")
            project = Project.objects(id=ObjectId(value)).first()
            if not project_accessible(project, actor):
                fail("关联项目不可访问", 403)
            projects.add(value)
            if len(projects) > 5:
                fail("最多插入 5 个项目名片")
            node.clear()
            node.update(type="project", project_id=value)
    return nodes


def visible_nodes(nodes, user):
    from app.models.project import Project

    # Resolve each distinct project once; never trust an actor's cached projection.
    ids = {ObjectId(n["project_id"]) for n in walk(nodes) if n["type"] == "project"}
    projects = {str(p.id): p for p in Project.objects(id__in=ids)} if ids else {}

    def convert(node):
        if node["type"] == "project":
            project = projects.get(node["project_id"])
            if project_accessible(project, user):
                return {
                    "type": "project",
                    "project_id": str(project.id),
                    "name": project.name,
                }
            return {"type": "unavailable"}
        result = dict(node)
        if "children" in node:
            result["children"] = [convert(c) for c in node["children"]]
        return result

    return [convert(n) for n in nodes]


def render_html(nodes):
    tags = {
        "b": "strong",
        "i": "em",
        "u": "u",
        "s": "s",
        "quote": "blockquote",
        "list": "ul",
        "li": "li",
    }
    out = []
    for n in nodes:
        kind = n["type"]
        if kind == "text":
            out.append(html.escape(n["text"]).replace("\n", "<br>"))
        elif kind in ("project", "unavailable"):
            out.append("登录后查看关联项目")
        elif kind == "url":
            out.append(
                '<a rel="noopener noreferrer" href="'
                + html.escape(n["url"], quote=True)
                + '">'
                + render_html(n["children"])
                + "</a>"
            )
        elif kind in tags:
            tag = tags[kind]
            out.append(f"<{tag}>" + render_html(n["children"]) + f"</{tag}>")
    return "".join(out)
