#!/usr/bin/env python3
"""
Notion → GitHub: читает страницы из Notion, сравнивает с файлами в git.
Если есть разница — создаёт branch и PR с diff для ревью.

Запуск: python sync_notion_to_github.py
Cron:   */60 * * * * cd /path/to/sync && python sync_notion_to_github.py
"""

import json
import os
import re
import hashlib
import requests
from datetime import datetime

# --- Config ---
def load_config():
    config_path = os.environ.get("SYNC_CONFIG", "sync_config.json")
    with open(config_path) as f:
        return json.load(f)

# --- Notion API ---
def notion_get_page_content(page_id: str, token: str) -> str:
    """Читает содержимое страницы из Notion и возвращает как markdown."""
    # Получаем блоки страницы
    url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28"
    }
    
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    blocks = response.json().get("results", [])
    
    return blocks_to_markdown(blocks, headers)


def blocks_to_markdown(blocks: list, headers: dict) -> str:
    """Конвертирует Notion blocks в markdown."""
    lines = []
    
    for block in blocks:
        block_type = block.get("type", "")
        
        if block_type == "heading_1":
            text = extract_rich_text(block["heading_1"]["rich_text"])
            lines.append(f"# {text}")
        
        elif block_type == "heading_2":
            text = extract_rich_text(block["heading_2"]["rich_text"])
            lines.append(f"## {text}")
        
        elif block_type == "heading_3":
            text = extract_rich_text(block["heading_3"]["rich_text"])
            lines.append(f"### {text}")
        
        elif block_type == "paragraph":
            text = extract_rich_text(block["paragraph"]["rich_text"])
            lines.append(text)
        
        elif block_type == "bulleted_list_item":
            text = extract_rich_text(block["bulleted_list_item"]["rich_text"])
            lines.append(f"- {text}")
        
        elif block_type == "numbered_list_item":
            text = extract_rich_text(block["numbered_list_item"]["rich_text"])
            lines.append(f"1. {text}")
        
        elif block_type == "to_do":
            text = extract_rich_text(block["to_do"]["rich_text"])
            checked = "x" if block["to_do"]["checked"] else " "
            lines.append(f"- [{checked}] {text}")
        
        elif block_type == "toggle":
            text = extract_rich_text(block["toggle"]["rich_text"])
            lines.append(f"<details>\n<summary>{text}</summary>\n</details>")
        
        elif block_type == "code":
            text = extract_rich_text(block["code"]["rich_text"])
            lang = block["code"].get("language", "")
            lines.append(f"```{lang}\n{text}\n```")
        
        elif block_type == "divider":
            lines.append("---")
        
        elif block_type == "callout":
            text = extract_rich_text(block["callout"]["rich_text"])
            icon = block["callout"].get("icon", {}).get("emoji", "💡")
            lines.append(f"> {icon} {text}")
        
        elif block_type == "quote":
            text = extract_rich_text(block["quote"]["rich_text"])
            lines.append(f"> {text}")
        
        elif block_type == "table":
            # Читаем дочерние блоки таблицы
            table_rows = get_children(block["id"], headers)
            for i, row in enumerate(table_rows):
                cells = row.get("table_row", {}).get("cells", [])
                row_text = " | ".join(
                    extract_rich_text(cell) for cell in cells
                )
                lines.append(f"| {row_text} |")
                if i == 0:
                    lines.append("|" + "|".join("---" for _ in cells) + "|")
        
        # Если блок имеет дочерние блоки — рекурсивно
        if block.get("has_children") and block_type not in ["table"]:
            children = get_children(block["id"], headers)
            child_md = blocks_to_markdown(children, headers)
            # Отступ для вложенных блоков
            for line in child_md.split("\n"):
                if line.strip():
                    lines.append(f"  {line}")
    
    return "\n\n".join(lines)


def get_children(block_id: str, headers: dict) -> list:
    """Получает дочерние блоки."""
    url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json().get("results", [])


def extract_rich_text(rich_text_array: list) -> str:
    """Извлекает текст из Notion rich_text массива."""
    parts = []
    for rt in rich_text_array:
        text = rt.get("plain_text", "")
        annotations = rt.get("annotations", {})
        
        if annotations.get("bold"):
            text = f"**{text}**"
        if annotations.get("italic"):
            text = f"*{text}*"
        if annotations.get("code"):
            text = f"`{text}`"
        if annotations.get("strikethrough"):
            text = f"~~{text}~~"
        
        href = rt.get("href")
        if href:
            text = f"[{text}]({href})"
        
        parts.append(text)
    
    return "".join(parts)


def notion_get_page_title(page_id: str, token: str) -> str:
    """Получает заголовок страницы."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    props = response.json().get("properties", {})
    
    for prop in props.values():
        if prop.get("type") == "title":
            return extract_rich_text(prop.get("title", []))
    
    return "Untitled"


# --- GitHub API ---
def github_get_file(repo: str, path: str, token: str, ref: str = "main") -> tuple:
    """Получает содержимое файла из GitHub. Возвращает (content, sha)."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    response = requests.get(url, headers=headers)
    
    if response.status_code == 404:
        return None, None
    
    response.raise_for_status()
    data = response.json()
    
    import base64
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def github_get_main_sha(repo: str, token: str) -> str:
    """Получает SHA последнего коммита в main."""
    url = f"https://api.github.com/repos/{repo}/git/ref/heads/main"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()["object"]["sha"]


def github_create_branch(repo: str, branch_name: str, sha: str, token: str):
    """Создаёт новую ветку."""
    url = f"https://api.github.com/repos/{repo}/git/refs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    data = {
        "ref": f"refs/heads/{branch_name}",
        "sha": sha
    }
    response = requests.post(url, headers=headers, json=data)
    
    # Если ветка уже существует — ок
    if response.status_code == 422:
        return
    response.raise_for_status()


def github_update_file(repo: str, path: str, content: str, 
                       branch: str, message: str, token: str, sha: str = None):
    """Создаёт или обновляет файл в GitHub."""
    import base64
    
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    data = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "branch": branch
    }
    
    if sha:
        data["sha"] = sha
    
    response = requests.put(url, headers=headers, json=data)
    response.raise_for_status()
    return response.json()


def github_create_pr(repo: str, branch: str, title: str, body: str, 
                     token: str, reviewer: str = None):
    """Создаёт Pull Request."""
    url = f"https://api.github.com/repos/{repo}/pulls"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    data = {
        "title": title,
        "head": branch,
        "base": "main",
        "body": body
    }
    response = requests.post(url, headers=headers, json=data)
    
    # Если PR уже существует — обновляем
    if response.status_code == 422:
        print(f"  PR для ветки {branch} уже существует")
        return None
    
    response.raise_for_status()
    pr = response.json()
    
    # Назначаем ревьюера
    if reviewer and pr:
        review_url = f"https://api.github.com/repos/{repo}/pulls/{pr['number']}/requested_reviewers"
        requests.post(review_url, headers=headers, json={"reviewers": [reviewer]})
    
    return pr


# --- Sync Logic ---
def content_hash(content: str) -> str:
    """Хэш содержимого для сравнения."""
    normalized = re.sub(r'\s+', ' ', content.strip())
    return hashlib.md5(normalized.encode()).hexdigest()


def sync_notion_to_github():
    """Основная функция синхронизации Notion → GitHub."""
    config = load_config()
    notion_token = os.environ.get("NOTION_TOKEN", config.get("notion_token", ""))
    github_token = os.environ.get("GITHUB_TOKEN", config.get("github_token", ""))
    repo = config["github_repo"]
    reviewer = config.get("reviewer")
    
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    changes = []
    
    print(f"[{timestamp}] Начинаю синхронизацию Notion → GitHub")
    print(f"  Repo: {repo}")
    print(f"  Страниц для проверки: {len(config['pages'])}")
    
    for page_config in config["pages"]:
        page_id = page_config["notion_page_id"]
        git_path = page_config["git_path"]
        title = page_config["title"]
        
        if page_id == "FILL_IN":
            print(f"  ⏭ {title}: page_id не заполнен, пропускаю")
            continue
        
        print(f"\n  Проверяю: {title} ({git_path})")
        
        # Читаем из Notion
        try:
            notion_content = notion_get_page_content(page_id, notion_token)
            notion_title = notion_get_page_title(page_id, notion_token)
        except Exception as e:
            print(f"  ❌ Ошибка чтения Notion: {e}")
            continue
        
        # Формируем markdown с заголовком
        full_notion_md = f"# {notion_title}\n\n{notion_content}\n"
        
        # Добавляем метаданные
        full_notion_md += f"\n---\n*Synced from Notion: {datetime.now().isoformat()}*\n"
        full_notion_md += f"*Notion page: https://notion.so/{page_id.replace('-', '')}*\n"
        
        # Читаем из GitHub
        try:
            git_content, git_sha = github_get_file(repo, git_path, github_token)
        except Exception as e:
            print(f"  ❌ Ошибка чтения GitHub: {e}")
            continue
        
        # Сравниваем (игнорируя метаданные синхронизации)
        def strip_sync_meta(text):
            if text is None:
                return ""
            return re.sub(r'\n---\n\*Synced from Notion:.*$', '', text, flags=re.DOTALL).strip()
        
        notion_clean = strip_sync_meta(full_notion_md)
        git_clean = strip_sync_meta(git_content) if git_content else ""
        
        if content_hash(notion_clean) == content_hash(git_clean):
            print(f"  ✓ {title}: без изменений")
            continue
        
        print(f"  📝 {title}: есть изменения, добавляю в PR")
        changes.append({
            "path": git_path,
            "content": full_notion_md,
            "sha": git_sha,
            "title": title,
            "page_id": page_id
        })
    
    if not changes:
        print(f"\n✓ Нет изменений для синхронизации")
        return
    
    # Создаём branch и PR
    branch_name = f"{config['branch_prefix']}/{timestamp}"
    main_sha = github_get_main_sha(repo, github_token)
    
    print(f"\n  Создаю ветку: {branch_name}")
    github_create_branch(repo, branch_name, main_sha, github_token)
    
    # Коммитим каждый изменённый файл
    changed_titles = []
    for change in changes:
        print(f"  Коммичу: {change['path']}")
        github_update_file(
            repo=repo,
            path=change["path"],
            content=change["content"],
            branch=branch_name,
            message=f"sync(notion): update {change['title']}",
            token=github_token,
            sha=change["sha"]
        )
        changed_titles.append(change["title"])
    
    # Создаём PR
    pr_title = f"🔄 Notion sync: {', '.join(changed_titles)}"
    pr_body = f"""## Автоматическая синхронизация из Notion

**Дата:** {datetime.now().strftime('%Y-%m-%d %H:%M')}
**Источник:** Notion AI Context Hub
**Изменённые файлы:**

"""
    for change in changes:
        pr_body += f"- `{change['path']}` ({change['title']})\n"
    
    pr_body += """
---

⚠️ **Этот PR создан автоматически скриптом синхронизации.**

Проверь diff, убедись что изменения корректны, и нажми Approve → Merge.

Если есть конфликт с изменениями в git — реши его вручную.
"""
    
    print(f"\n  Создаю PR: {pr_title}")
    pr = github_create_pr(repo, branch_name, pr_title, pr_body, github_token, reviewer)
    
    if pr:
        print(f"\n✅ PR создан: {pr['html_url']}")
        print(f"   Ревьюер: {reviewer}")
    else:
        print(f"\n⚠️ PR уже существует для этой ветки")


if __name__ == "__main__":
    sync_notion_to_github()
