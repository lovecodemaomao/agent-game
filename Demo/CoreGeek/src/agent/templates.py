"""任务模板库: 把"按题型一次做完"的确定性流程从 LLM 往返里剥离出来。

竞赛复盘(TeamA 22 回合 vs TeamB 36 回合+)显示三个差距, 全部是"回合被浪费在
可以事先写死的动作上":

  1) 任务文件定位 —— TeamB 每题先花 3-6 回合 find 探索; 题干其实已经给出了文件名。
     -> locate_command(): 一轮内"定位 + 列目录 + 打印正文"。
  2) 修复部署类任务分步执行, 且漏掉"检查脚本带 Windows 回车符"这一步而返工。
     -> engineer_command(): 把题干列出的步骤拼成一条自包含命令, 去 CR 后再跑检查。
  3) API 类任务用回合去试错鉴权头/参数名(Authorization vs X-API-Key, location vs city)。
     -> api_command(): 一条命令内把所有组合试完并聚合全量记录。

模板只在题干足以判定题型时启用(修复类要求"第N行+check", 查询类要求 http 地址
且带答题字段/任务目录语境), 其余任务仍走"定位 -> 知识 prompt -> LLM"的通用流程。
"""
import base64
import json
import re

TASK_DIR = '/tmp/selfEvolutionTask/'
HERITAGE_API_KEY = 'heritage-api-key-2024'

# 各题型的回合预算(用于"时间不够就不接这个任务"的死线管理)
ENGINEER_ROUNDS = 5
API_ROUNDS = 6
UNKNOWN_ROUNDS = 20

_PATH_RE = re.compile(r'(/[\w./-]*ws[_-]?\d+/?)')
_FILE_RE = re.compile(r'[\w./-]+\.(?:md|conf|cfg|ini|sh|py|json|txt|log)')
_LINE_RE = re.compile(r'第\s*(\d+)\s*行')
_LOG_RE = re.compile(r'logs/([\w.-]+)')
_URL_RE = re.compile(r'https?://[\w.:%/?=&-]+')
_PATH_FRAG_RE = re.compile(r'/(?:api|v\d)[\w./-]*')
_TOKEN_RE = re.compile(r'TOKEN\s*[:：=]\s*([A-Za-z0-9_.-]{4,})')
_CITY_RE = re.compile(r'(?:查询|获取|统计|搜索|列出)?([\u4e00-\u9fa5]{2,6}?)(?:市|省)?(?:的)?'
                      r'(?:全部|所有|今天|当天|明天|文化遗产|文物|天气|记录|信息)')
_JSON_RE = re.compile(r'\{[^{}]*\}')
_QUOTED_RE = re.compile(r'["“”\'‘’`《]([\u4e00-\u9fa5]{2,10})["“”\'‘’`》]')
_KEY_RE = re.compile(r"[a-z][a-z0-9_]{1,29}")
_HARVEST_RE = re.compile(r'__API\s+(\{.*\})')
# 只有"记录列表型"接口才用采集模板; 单接口文档型(如 ?city=<城市名> 的天气)走 doc_query
_RECORD_HINT = re.compile(r'记录|列表|清单|全部|所有|文物|遗产|records|list')
_PARAM_RE = re.compile(r'[?&]([A-Za-z_]\w*)=')

# 答题字段前缀 -> 中文值的对照(用于把 *_count 这类字段和记录里的取值对上)
_ALIAS = (('world', '世界'), ('heritage', '遗产'), ('cultural', '文化'),
          ('relic', '文物'), ('national', '国家级'), ('province', '省级'),
          ('protection', '保护'), ('level', '级别'), ('unit', '单位'),
          ('key', '重点'), ('city', '市'))
# 中国年代先后(仅用于"年代最早的遗产名称"这类问题, 全部取值都能对上才作答)
_ERA_ORDER = ('旧石器', '新石器', '史前', '夏', '商', '西周', '东周', '春秋', '战国',
              '秦', '西汉', '东汉', '汉', '三国', '西晋', '东晋', '晋', '南北朝',
              '隋', '唐', '五代', '北宋', '南宋', '宋', '辽', '金', '元', '明', '清',
              '近代', '现代')

# 在沙盒里跑的采集脚本: 一条命令内试完 鉴权头 x 参数名 x 候选地址, 并翻页取全量。
# 只用双引号, 避免与 shell 单引号转义冲突; 通过环境变量接收参数。
HARVEST = '''import json,os,time,urllib.parse,urllib.request
BASE=os.environ.get("BASE","").rstrip("/")
CITY=os.environ.get("CITY","")
KEY=os.environ.get("KEY","")
PARAMS=[]
for name in os.environ.get("PARAMS","").split(",")+["location","city"]:
    if name and name not in PARAMS:
        PARAMS.append(name)
SEEN=[]
for p in [BASE]+[x for x in os.environ.get("PATHS","").split(",") if x]:
    p=p if p.startswith("http") else BASE+p
    if p and p not in SEEN:
        SEEN.append(p)
DEADLINE=time.time()+11
def fetch(url,auth):
    req=urllib.request.Request(url,headers={auth:("Bearer "+KEY if auth=="Authorization" else KEY)})
    with urllib.request.urlopen(req,timeout=2.5) as resp:
        return json.loads(resp.read().decode("utf-8","replace"))
def records(obj):
    if isinstance(obj,list):
        return obj
    if isinstance(obj,dict):
        for k in ("data","records","items","list","result","rows","content","results"):
            v=obj.get(k)
            if isinstance(v,list):
                return v
            if isinstance(v,dict):
                for k2 in ("records","items","list","rows","data","results"):
                    v2=v.get(k2)
                    if isinstance(v2,list):
                        return v2
    return []
out={"status":"FAIL","tried":[]}
done=False
for auth in ("Authorization","X-API-Key"):
    if done or time.time()>DEADLINE:
        break
    for pname in PARAMS:
        if done or time.time()>DEADLINE:
            break
        for path in SEEN:
            if time.time()>DEADLINE:
                break
            # urllib 要求 URL 为 ASCII, 因此这里必须百分号编码(curl 模板才用未编码形式)
            url=path+("&" if "?" in path else "?")+urllib.parse.urlencode({pname:CITY,"limit":100})
            try:
                raw=fetch(url,auth)
            except Exception as exc:
                out["tried"].append({"auth":auth,"param":pname,"path":path,"err":str(exc)[:120]})
                continue
            rows=records(raw)
            if not rows:
                # 单对象接口(如天气): 请求成功也算命中, 把原文交给上层/LLM
                out={"status":"OK","auth":auth,"param":pname,"path":path,"count":0,
                     "records":[],"flat":raw,"raw_head":json.dumps(raw,ensure_ascii=False)[:1200]}
                if isinstance(raw,dict) and raw:
                    done=True
                    break
                out["tried"].append({"auth":auth,"param":pname,"path":path,"empty":True})
                continue
            pages=1
            while pages<10 and len(rows)>=100 and len(rows)%100==0 and time.time()<DEADLINE:
                more=[]
                for key,val in (("offset",len(rows)),("page",pages+1),("start",len(rows))):
                    if time.time()>DEADLINE:
                        break
                    u2=path+("&" if "?" in path else "?")+urllib.parse.urlencode({pname:CITY,"limit":100,key:val})
                    try:
                        extra=records(fetch(u2,auth))
                    except Exception:
                        extra=[]
                    if len(extra)>len(more):
                        more=extra
                if not more:
                    break
                rows=rows+more
                pages=pages+1
            out={"status":"OK","auth":auth,"param":pname,"path":path,"count":len(rows),
                 "pages":pages,"records":rows,"raw_head":json.dumps(raw,ensure_ascii=False)[:1200]}
            done=True
            break
print("__API "+json.dumps(out,ensure_ascii=False))
'''


# ------------------------------------------------------------------ 题干信息抽取
def mentioned_files(text):
    out = []
    for match in _FILE_RE.finditer(str(text or '')):
        value = match.group(0)
        if value not in out:
            out.append(value)
    return out


def workdir(desc, dump=''):
    """任务工作目录: 题干里的 .../ws_N/ 路径优先, 其次定位输出里的路径。"""
    for text in (desc, dump):
        match = _PATH_RE.search(str(text or ''))
        if match:
            return match.group(1)
    return ''


def city_name(text):
    match = _CITY_RE.search(str(text or ''))
    if not match:
        return ''
    # 题干里的动词/敬语可能被贪婪的"2-6 个汉字"吃掉(如"请查询成都"), 这里剥掉
    name = re.sub(r'^(?:请|帮我|需要|麻烦|帮忙)*(?:查询|获取|统计|搜索|列出|调用)?',
                  '', match.group(1))
    return name.strip()


def conf_edits(text):
    """解析"把 <file> 第N行改为 <value>"这类要求 -> [(file, line, value)]。"""
    text = str(text or '').replace('；', '\n').replace(';', '\n')
    files = mentioned_files(text)
    default = next((f for f in files if f.endswith(('.conf', '.cfg', '.ini'))), '')
    edits = {}
    for raw in text.split('\n'):
        line = _LINE_RE.search(raw)
        if not line:
            continue
        index = raw.rfind('为')
        if index < 0:
            continue
        value = raw[index + 1:].strip().strip('。.,，、')
        value = value.strip('`\'"“”‘’')
        if not value:
            continue
        conf = next((f for f in mentioned_files(raw)
                     if f.endswith(('.conf', '.cfg', '.ini'))), default)
        if not conf:
            continue
        edits[(conf, int(line.group(1)))] = value
    return [(path, line, value) for (path, line), value in edits.items()]


def shell_files(text):
    return [f for f in mentioned_files(text) if f.endswith('.sh')]


def answer_keys(text):
    """从"答题格式"里取字段名: JSON 样例里的键 + 反引号包裹的 snake_case 词。"""
    keys = []
    for raw in _JSON_RE.findall(str(text or '')):
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        for key in value:
            if isinstance(key, str) and _KEY_RE.fullmatch(key) and key not in keys:
                keys.append(key)
    for key in _KEY_RE.findall(' '.join(re.findall(r'`([^`]{1,60})`', str(text or '')))):
        if key not in keys:
            keys.append(key)
    return keys


def quoted_phrases(text):
    out = []
    for phrase in _QUOTED_RE.findall(str(text or '')):
        if phrase not in out:
            out.append(phrase)
    return out


# ------------------------------------------------------------------ 模板一: 定位
def locate_command(desc):
    """一轮内定位任务文件: 题干给了文件名就精确找, 否则退回目录结构探测。"""
    names = [f for f in mentioned_files(desc) if f.endswith(('.md', '.txt', '.json'))]
    if not names:
        return ("find %s -maxdepth 3 2>/dev/null | head -80; echo '---'; "
                "find %s -maxdepth 3 -type f -size -64k 2>/dev/null | head -20 | "
                "while read f; do echo \"===== $f =====\"; cat \"$f\"; done | head -400"
                % (TASK_DIR, TASK_DIR))
    name = names[0]
    return ('f=$( [ -f "%s" ] && echo "%s" || find %s -maxdepth 4 -name "%s" 2>/dev/null | head -1); '
            'echo "FILE=$f"; if [ -n "$f" ]; then d=$(dirname "$f"); echo "DIR=$d"; '
            'ls -la "$d"; echo "===== $f ====="; cat "$f"; fi'
            % (name, name, TASK_DIR, name))


# ------------------------------------------------------------------ 模板二: 修复部署
def engineer_command(desc, dump=''):
    """修复部署类: 一条命令建目录/改配置/加权限/去回车/跑检查。"""
    text = '%s\n%s' % (desc or '', dump or '')
    if 'check' not in text.lower():
        return None
    edits = conf_edits(text)
    if not edits:
        return None
    steps = []
    root = workdir(desc, dump)
    if root:
        steps.append('cd "%s"' % root)
    log = _LOG_RE.search(text)
    if log:
        steps.append('mkdir -p logs/%s' % log.group(1))
        steps.append('chmod 755 logs/%s' % log.group(1))
    for path, line, value in edits:
        # sed 分隔符用 | , 并转义替换串里的 & 与 |
        literal = value.replace('\\', '\\\\').replace('&', '\\&').replace('|', '\\|')
        steps.append("sed -i.bak '%ds|.*|%s|' %s" % (line, literal, path))
    for path in dict.fromkeys([p for p, _, _ in edits]):
        steps.append('rm -f %s.bak' % path)
    for path in shell_files(text):
        steps.append('chmod 755 %s' % path)
    # 关键: 检查脚本先去掉 Windows 回车, 再加执行位 —— 漏掉这步会白白多花 2 个回合
    steps.append("sed -i.bak 's/\\r$//' check")
    steps.append('rm -f check.bak')
    steps.append('chmod +x check')
    steps.append('sh ./check; echo "CHECK_EXIT=$?"')
    return ' && '.join(steps)


# ------------------------------------------------------------------ 模板三: API 查询
def api_command(desc, dump=''):
    """记录列表型接口: 一条命令内试完所有鉴权/参数组合并翻页取全量记录。"""
    text = str(desc or '')
    urls = [u.rstrip('.,;:)\'"') for u in _URL_RE.findall(text)]
    if not urls:
        return None
    corpus = '%s\n%s' % (text, dump or '')
    # 仅限"记录列表"语境: 否则可能把单接口文档型任务误判成采集任务(白花一个回合)
    if not _RECORD_HINT.search(corpus):
        return None
    paths = []
    for path in _PATH_FRAG_RE.findall(corpus):
        if path.rstrip('/') not in paths:
            paths.append(path.rstrip('/'))
    for url in _URL_RE.findall(corpus):
        url = url.rstrip('.,;:)\'"')
        if url not in paths:
            paths.append(url)
    params = []
    for name in _PARAM_RE.findall(corpus) + ['location', 'city']:
        if name not in params:
            params.append(name)
    encoded = base64.b64encode(HARVEST.encode('utf-8')).decode('ascii')
    return ("mkdir -p /tmp/skills && printf '%%s' '%s' | base64 -d > /tmp/skills/harvest.py && "
            "BASE='%s' CITY='%s' KEY='%s' PATHS='%s' PARAMS='%s' python3 /tmp/skills/harvest.py"
            % (encoded, urls[0].rstrip('/'), city_name(text), HERITAGE_API_KEY,
               ','.join(paths), ','.join(params)))


def doc_query_command(desc, dump=''):
    """题干直接给了接口文档(GET <url>?<param>=<取值/占位符>)时, 按文档原样调用一次。

    只填文档里出现的参数名, 取值只来自题干里的城市名 —— 不臆造接口和参数。
    """
    text = '%s\n%s' % (desc or '', dump or '')
    if not re.search(r'curl|GET|接口|API|api', text):
        return None
    city = city_name(desc) or city_name(dump or '')
    for raw in _URL_RE.findall(text):
        url = raw.rstrip('.,;:)\'"')
        if '?' not in url:
            continue
        base, _, query = url.partition('?')
        parts = []
        for name in _PARAM_RE.findall('?%s' % query):
            if name not in parts:
                parts.append(name)
        if not parts:
            continue
        filled = []
        for name in parts:
            if city and re.search(r'city|location|name|area|region|城市|地名', name, re.I):
                filled.append('%s=%s' % (name, city))
        if not filled:
            continue
        # 中文取值必须百分号编码: 直接把中文写进 URL, Python/Flask 服务端按 latin-1
        # 解请求行, 会收到乱码(实测会白白多花 2 个回合)。
        return 'curl -s -G "%s" %s' % (base, ' '.join('--data-urlencode "%s"' % item
                                                     for item in filled))
    return None


# ------------------------------------------------------------------ 调度
def plan(desc, dump=''):
    """按题型给出"一条命令做完"的模板; 题型无法判定时返回 None。"""
    return (engineer_command(desc, dump) or api_command(desc, dump)
            or doc_query_command(desc, dump))


def kind(desc, dump=''):
    for name, builder in (('engineering', engineer_command), ('api', api_command),
                          ('api-doc', doc_query_command)):
        if builder(desc, dump):
            return name
    return 'unknown'


def estimate(desc, dump=''):
    """该任务大概需要多少回合(用于接任务前的死线管理)。"""
    return {'engineering': ENGINEER_ROUNDS, 'api': API_ROUNDS,
            'api-doc': API_ROUNDS}.get(kind(desc, dump), UNKNOWN_ROUNDS)


# ------------------------------------------------------------------ 结果解析
def parse_harvest(text):
    match = _HARVEST_RE.search(str(text or ''))
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _as_text(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _count_by_phrase(low, records, corpus):
    prefix = re.sub(r'_(count|num|number|total)$', '', low)
    tokens = [t for t in prefix.split('_') if t]
    if not tokens:
        return None
    best = None
    for phrase in quoted_phrases(corpus):
        score = sum(1 for token in tokens for en, zh in _ALIAS if en in token and zh in phrase)
        if score and (best is None or score > best[0]):
            best = (score, phrase)
    if best is None:
        return None
    phrase = best[1]
    return sum(1 for row in records
               if any(_as_text(value) == phrase for value in row.values()))


def _fields(records):
    """记录字段名(按出现顺序, 保证判定可复现)。"""
    names = []
    for row in records:
        for name in row:
            if name not in names:
                names.append(name)
    return names


def _categories(records):
    total = len(records)
    best = None
    for name in _fields(records):
        values = []
        for row in records:
            text = _as_text(row.get(name))
            if text and text not in values:
                values.append(text)
        if len(values) < 2 or len(values) > 40 or len(values) == total:
            continue
        if any(len(v) > 16 for v in values):
            continue
        label = str(name).lower()
        score = 3 if ('type' in label or 'categor' in label or 'kind' in label
                      or '类型' in str(name)) else 1
        if best is None or score > best[0]:
            best = (score, values)
    return best[1] if best else None


def _era_rank(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _as_text(value)
    ranks = [i for i, era in enumerate(_ERA_ORDER) if era in text]
    return min(ranks) if ranks else None


def _oldest_name(records):
    field = None
    for name in _fields(records):
        label = str(name).lower()
        if any(hint in label for hint in ('era', 'year', 'dynasty')) \
                or '年代' in str(name) or '时期' in str(name):
            field = name
            break
    if field is None:
        for name in _fields(records):
            values = [row.get(name) for row in records]
            if values and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                              for v in values) and len(set(values)) > 1 \
                    and 1 <= min(values) and max(values) <= 2100:
                field = name
                break
    if field is None:
        return None
    scored = []
    for row in records:
        rank = _era_rank(row.get(field))
        if rank is None:
            return None
        scored.append((rank, row))
    _, row = min(scored, key=lambda item: item[0])
    for name in ('name', 'title', '名称'):
        if isinstance(row.get(name), str) and row[name]:
            return row[name]
    for name, value in row.items():
        if name != field and isinstance(value, str) and value:
            return value
    return None


def answer_from_harvest(keys, records, city, corpus=''):
    """把采集到的记录映射成答题字段; 有任何一个字段证不出来就返回 None(交给 LLM)。"""
    if not keys or not isinstance(records, list) or not records:
        return None
    if not all(isinstance(row, dict) and row for row in records):
        return None
    answer = {}
    for key in keys:
        low = key.lower()
        if low in ('city', 'location', 'region', '城市') or low.endswith('_city'):
            value = city or next((_as_text(row.get(f)) for row in records
                                  for f in ('city', 'location') if row.get(f)), '')
            if not value:
                return None
            answer[key] = value
        elif low.startswith('total') or low in ('count', 'num', 'number', 'size', 'length'):
            answer[key] = len(records)
        elif low.endswith(('_count', '_num', '_number')):
            value = _count_by_phrase(low, records, corpus)
            if value is None:
                return None
            answer[key] = value
        elif low in ('types', 'categories', 'kinds', 'type_list') or low.endswith('_types'):
            value = _categories(records)
            if value is None:
                return None
            answer[key] = value
        elif 'oldest' in low or low.endswith('_era') or low in ('era', 'dynasty'):
            value = _oldest_name(records)
            if value is None:
                return None
            answer[key] = value
        else:
            return None
    if not answer or any(value in (None, '', []) for value in answer.values()):
        return None
    return answer


def derive_answer(desc, dump, result):
    """沙盒输出 -> 直接可提交的答案(省掉一次 LLM 往返); 无法确定时返回 None。"""
    text = str(result or '')
    token = _TOKEN_RE.search(text)
    if token:
        return token.group(1)
    harvest = parse_harvest(text)
    if not harvest or harvest.get('status') != 'OK':
        return None
    keys = answer_keys(dump or '') or answer_keys(desc)
    answer = answer_from_harvest(keys, harvest.get('records') or [],
                                 city_name(desc) or city_name(dump or ''),
                                 '%s\n%s' % (desc or '', dump or ''))
    if answer is None:
        return None
    return json.dumps(answer, ensure_ascii=False)
