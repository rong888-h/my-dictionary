from flask import Flask, render_template, request, jsonify
import requests
import sqlite3
import time
import re

app = Flask(__name__)

# --- 1. 初始化数据库（创建计数表） ---
def init_db():
    conn = sqlite3.connect('words.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history 
                 (word TEXT PRIMARY KEY, count INTEGER, last_time TEXT)''')
    conn.commit()
    conn.close()

# --- 2. 从本地词库查询（SQLite） ---
def get_word_from_db(word):
    conn = sqlite3.connect('vocabulary.db')
    cursor = conn.cursor()
    cursor.execute('''
    SELECT word, phonetic, definition, translation, pos, collins, oxford, tag, bnc, frq, exchange, detail, audio
    FROM words 
    WHERE word = ?
    ''', (word,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        return {
            "word": result[0],
            "phonetic": result[1],
            "definition": result[2],
            "translation": result[3],
            "pos": result[4],
            "collins": result[5],
            "oxford": result[6],
            "tag": result[7],
            "bnc": result[8],
            "frq": result[9],
            "exchange": result[10],
            "detail": result[11],
            "audio": result[12]
        }
    return None

# --- 3. 获取发音URL（使用有道发音，更稳定） ---
def get_audio_url(word):
    return f"http://dict.youdao.com/dictvoice?type=1&audio={word}"

# --- 4. 解析英文释义，按词性分组换行展示 ---
def parse_definitions_by_pos(definition_text):
    """将英文释义按词性分组，返回列表"""
    if not definition_text:
        return []
    
    result = []
    
    # 先按 \n 分割
    text = definition_text.replace('\\n', '\n')
    lines = text.split('\n')
    
    current_pos = None
    current_text = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # 检查是否是词性开头（n. v. adj. adv. 等）
        pos_match = re.match(r'^([a-zA-Z]+\.)\s*(.*)$', line)
        if pos_match:
            # 保存上一个词性的内容
            if current_pos and current_text:
                result.append({
                    "pos": current_pos,
                    "text": ' '.join(current_text).strip()
                })
            # 开始新的词性
            current_pos = pos_match.group(1)
            current_text = [pos_match.group(2).strip()] if pos_match.group(2) else []
        else:
            # 没有词性标识，可能是续行
            if current_text:
                current_text.append(line)
            else:
                # 如果没有当前词性，尝试用默认
                current_pos = "释义"
                current_text.append(line)
    
    # 保存最后一个
    if current_pos and current_text:
        result.append({
            "pos": current_pos,
            "text": ' '.join(current_text).strip()
        })
    
    # 如果没有解析到任何词性，尝试按数字编号分割
    if not result:
        # 按 1. 2. 3. 分割
        parts = re.split(r'(\d+\.\s*)', definition_text.replace('\\n', ' ').replace('\n', ' '))
        if len(parts) > 1:
            for i in range(1, len(parts), 2):
                num = parts[i].strip()
                text = parts[i+1].strip() if i+1 < len(parts) else ''
                if text:
                    result.append({
                        "pos": num,
                        "text": text
                    })
        else:
            # 无法分割，整体返回
            result.append({
                "pos": "释义",
                "text": definition_text.replace('\\n', ' ').replace('\n', ' ')
            })
    
    return result

# --- 5. 解析词形变化（显示为中文标签） ---
def parse_exchange(exchange_text):
    """将词形变化解析为中文标签+值"""
    if not exchange_text:
        return []
    
    result = []
    
    # 词形变化映射表：英文标识 -> 中文标签
    mapping = {
        's': '复数',
        'p': '复数',
        'pl': '复数',
        'past': '过去式',
        'pst': '过去式',
        'pp': '过去分词',
        'pastp': '过去分词',
        'presp': '现在分词',
        'ing': '现在分词',
        'third': '第三人称单数',
        '3': '第三人称单数',
        'comp': '比较级',
        'super': '最高级',
        'npl': '名词复数',
        'vpl': '动词复数',
    }
    
    # 尝试按逗号、分号或空格分割
    parts = re.split(r'[,，;；\s]+', exchange_text)
    parts = [p.strip() for p in parts if p.strip()]
    
    for part in parts:
        # 尝试匹配 key:value 格式
        match = re.match(r'^([a-zA-Z]+)\s*[:：]\s*(.+)$', part)
        if match:
            key = match.group(1).lower()
            value = match.group(2).strip()
            label = mapping.get(key, key)
            result.append({"label": label, "value": value})
        else:
            # 尝试匹配 key=value 或 key value 格式
            match2 = re.match(r'^([a-zA-Z]+)\s*=\s*(.+)$', part)
            if match2:
                key = match2.group(1).lower()
                value = match2.group(2).strip()
                label = mapping.get(key, key)
                result.append({"label": label, "value": value})
            else:
                # 检查是否包含常见词形标识
                found = False
                for key, label in mapping.items():
                    if key in part.lower():
                        value_part = re.sub(rf'^{key}\s*[:：]?\s*', '', part, flags=re.IGNORECASE)
                        result.append({"label": label, "value": value_part.strip() or part})
                        found = True
                        break
                if not found:
                    result.append({"label": "词形", "value": part})
    
    # 去重
    unique_result = []
    seen = set()
    for item in result:
        key = f"{item['label']}:{item['value']}"
        if key not in seen:
            seen.add(key)
            unique_result.append(item)
    
    return unique_result

# --- 6. 查词核心逻辑 ---
def fetch_word(word):
    db_result = get_word_from_db(word)
    
    if not db_result:
        return None
    
    audio_url = get_audio_url(word)
    
    # 解析英文释义（按词性分组）
    definitions = parse_definitions_by_pos(db_result.get('definition', ''))
    
    # 解析词形变化
    exchanges = parse_exchange(db_result.get('exchange', ''))
    
    return {
        "word": word,
        "audio": audio_url,
        "phonetic": db_result.get('phonetic', ''),
        "translation": db_result.get('translation', ''),
        "definitions": definitions,  # 按词性分组的释义列表
        "exchanges": exchanges,
        "collins": db_result.get('collins', ''),
        "oxford": db_result.get('oxford', ''),
        "has_dict": True
    }

# --- 7. 更新搜索计数 ---
def update_count(word):
    conn = sqlite3.connect('words.db')
    c = conn.cursor()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    c.execute("INSERT INTO history (word, count, last_time) VALUES (?, 1, ?) ON CONFLICT(word) DO UPDATE SET count = count + 1, last_time = excluded.last_time", (word, now))
    conn.commit()
    c.execute("SELECT count FROM history WHERE word = ?", (word,))
    count = c.fetchone()[0]
    conn.close()
    return count

# --- 8. 前端页面 ---
@app.route('/')
def index():
    return render_template('index.html')

# --- 9. API接口 ---
@app.route('/search')
def search():
    word = request.args.get('word', '').strip().lower()
    if not word:
        return jsonify({"error": "请输入单词"})
    
    result = fetch_word(word)
    if not result:
        return jsonify({"error": "未找到该单词，请检查拼写"})
    
    count = update_count(word)
    result["search_count"] = count
    return jsonify(result)

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=10000)