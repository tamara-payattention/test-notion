#!/usr/bin/env python3
"""
GitHub → Notion: после merge PR в main, обновляет соответствующие страницы в Notion.

Запуск: python sync_github_to_notion.py [список изменённых файлов]
Обычно вызывается из GitHub Action после merge.
"""

import json
import os
import re
import sys
import requests
from datetime import datetime


def load_config():
    config_path = os.environ.get("SYNC_CONFIG", "sync_config.json")
    with open(config_path) as f:
        return json.load(f)


def read_local_file(path: str) -> str:
    """Читает локальный файл."""
    with open(path, encoding="utf-8") as f:
        return f.read()


def markdown_to_notion_blocks(markdown: str) -> list:
    """Конвертирует markdown в Notion blocks для API."""
    blocks = []
    lines = markdown.split("\n")
    i = 0
    
    while i < len(lines):
        line = lines[i]
        
        # Пропускаем метаданные синхронизации
        if line.startswith("*Synced from Notion:") or line.startswith("*Notion page:"):
            i += 1
            continue
        
        # Заголовки
        if line.startswith("### "):
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": parse_inline_markdown(line[4:])}
            })
        elif line.startswith("## "):
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": parse_inline_markdown(line[3:])}
            })
        elif line.startswith("# "):
            # Пропускаем первый H1 — это title страницы
            if i == 0:
                i += 1
                continue
            blocks.append({
                "object": "block",
                "type": "heading_1",
                "heading_1": {"rich_text": parse_inline_markdown(line[2:])}
            })
        
        # Чекбоксы
        elif re.match(r'^- \[([ x])\] ', line):
            match = re.match(r'^- \[([ x])\] (.+)', line)
            checked = match.group(1) == "x"
            text = match.group(2)
            blocks.append({
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": parse_inline_markdown(text),
                    "checked": checked
                }
            })
        
        # Списки
        elif line.startswith("- "):
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": parse_inline_markdown(line[2:])}
            })
        elif re.match(r'^\d+\. ', line):
            text = re.sub(r'^\d+\. ', '', line)
            blocks.append({
                "object": "block",
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": parse_inline_markdown(text)}
            })
        
        # Код
        elif line.startswith("```"):
            lang = line[3:].strip() or "plain text"
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            blocks.append({
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": [{"type": "text", "text": {"content": "\n".join(code_lines)}}],
                    "language": lang
                }
            })
        
        # Цитата
        elif line.startswith("> "):
            blocks.append({
                "object": "block",
                "type": "quote",
                "quote": {"rich_text": parse_inline_markdown(line[2:])}
            })
        
        # Разделитель
        elif line.strip() == "---":
            blocks.append({
                "object": "block",
                "type": "divider",
                "divider": {}
            })
        
        # Параграф
        elif line.strip():
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": parse_inline_markdown(line)}
            })
        
        i += 1
    
    return blocks


def parse_inline_markdown(text: str) -> list:
    """Парсит inline markdown (bold, italic, code, links) в Notion rich_text."""
    if not text.strip():
        return [{"type": "text", "text": {"content": ""}}]
    
    # Упрощённый парсинг — покрывает основные случаи
    result = []
    
    # Паттерн для ссылок, bold, italic, code
    pattern = re.compile(
        r'(\*\*(.+?)\*\*)'   # bold
        r'|(\*(.+?)\*)'       # italic
        r'|(`(.+?)`)'         # code
        r'|(\[(.+?)\]\((.+?)\))'  # link
    )
    
    last_end = 0
    for match in pattern.finditer(text):
        # Текст до совпадения
        if match.start() > last_end:
            result.append({
                "type": "text",
                "text": {"content": text[last_end:match.start()]}
            })
        
        if match.group(2):  # bold
            result.append({
                "type": "text",
                "text": {"content": match.group(2)},
                "annotations": {"bold": True}
            })
        elif match.group(4):  # italic
            result.append({
                "type": "text",
                "text": {"content": match.group(4)},
                "annotations": {"italic": True}
            })
        elif match.group(6):  # code
            result.append({
                "type": "text",
                "text": {"content": match.group(6)},
                "annotations": {"code": True}
            })
        elif match.group(8):  # link
            result.append({
                "type": "text",
                "text": {
                    "content": match.group(8),
                    "link": {"url": match.group(9)}
                }
            })
        
        last_end = match.end()
    
    # Остаток текста
    if last_end < len(text):
        result.append({
            "type": "text",
            "text": {"content": text[last_end:]}
        })
    
    return result if result else [{"type": "text", "text": {"content": text}}]


def notion_clear_page(page_id: str, token: str):
    """Удаляет все блоки со страницы."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28"
    }
    
    url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    
    for block in response.json().get("results", []):
        delete_url = f"https://api.notion.com/v1/blocks/{block['id']}"
        requests.delete(delete_url, headers=headers)


def notion_append_blocks(page_id: str, blocks: list, token: str):
    """Добавляет блоки на страницу (батчами по 100)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    
    # Notion API ограничивает 100 блоков за раз
    for i in range(0, len(blocks), 100):
        batch = blocks[i:i+100]
        response = requests.patch(url, headers=headers, json={"children": batch})
        response.raise_for_status()


def notion_update_title(page_id: str, title: str, token: str):
    """Обновляет заголовок страницы."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    
    url = f"https://api.notion.com/v1/pages/{page_id}"
    data = {
        "properties": {
            "title": {
                "title": [{"type": "text", "text": {"content": title}}]
            }
        }
    }
    response = requests.patch(url, headers=headers, json=data)
    response.raise_for_status()


def sync_file_to_notion(git_path: str, page_id: str, token: str, repo_root: str = "."):
    """Синхронизирует один файл из git в Notion."""
    full_path = os.path.join(repo_root, git_path)
    
    if not os.path.exists(full_path):
        print(f"  ❌ Файл не найден: {full_path}")
        return False
    
    content = read_local_file(full_path)
    
    # Извлекаем title из первого H1
    title_match = re.match(r'^# (.+)', content)
    title = title_match.group(1) if title_match else os.path.basename(git_path)
    
    # Убираем метаданные синхронизации
    content = re.sub(r'\n---\n\*Synced from Notion:.*$', '', content, flags=re.DOTALL)
    
    # Конвертируем в Notion blocks
    blocks = markdown_to_notion_blocks(content)
    
    if not blocks:
        print(f"  ⚠️ Нет блоков для записи")
        return False
    
    # Очищаем страницу и записываем новый контент
    print(f"  Очищаю страницу {page_id}...")
    notion_clear_page(page_id, token)
    
    print(f"  Записываю {len(blocks)} блоков...")
    notion_append_blocks(page_id, blocks, token)
    
    # Обновляем title
    notion_update_title(page_id, title, token)
    
    print(f"  ✅ Обновлено: {title}")
    return True


def sync_github_to_notion(changed_files: list = None):
    """Основная функция: GitHub → Notion."""
    config = load_config()
    notion_token = os.environ.get("NOTION_TOKEN", config.get("notion_token", ""))
    repo_root = os.environ.get("REPO_ROOT", ".")
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[{timestamp}] Начинаю синхронизацию GitHub → Notion")
    
    # Строим маппинг path → page_id
    path_to_page = {}
    for page_config in config["pages"]:
        if page_config["notion_page_id"] != "FILL_IN":
            path_to_page[page_config["git_path"]] = page_config["notion_page_id"]
    
    # Если переданы конкретные файлы — синхронизируем только их
    # Если нет — синхронизируем все из маппинга
    files_to_sync = changed_files if changed_files else list(path_to_page.keys())
    
    synced = 0
    for git_path in files_to_sync:
        if git_path not in path_to_page:
            print(f"  ⏭ {git_path}: нет в маппинге, пропускаю")
            continue
        
        page_id = path_to_page[git_path]
        print(f"\n  Синхронизирую: {git_path} → Notion ({page_id})")
        
        try:
            if sync_file_to_notion(git_path, page_id, notion_token, repo_root):
                synced += 1
        except Exception as e:
            print(f"  ❌ Ошибка: {e}")
    
    print(f"\n✅ Синхронизировано файлов: {synced}/{len(files_to_sync)}")


if __name__ == "__main__":
    # Принимаем список файлов из аргументов (GitHub Action передаёт их)
    changed_files = sys.argv[1:] if len(sys.argv) > 1 else None
    sync_github_to_notion(changed_files)
