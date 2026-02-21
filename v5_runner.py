import json
import os
import re
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from difflib import get_close_matches
from apply_patch import apply_patches

# --------------------------------------------------
# LOAD AGENT INSTRUCTIONS
# --------------------------------------------------
DEV_ALWAYS_NEW = True

from dotenv import load_dotenv
load_dotenv()

from google import genai

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def project_path(*parts):
    return os.path.join(BASE_DIR, *parts)

AGENT_SPEC_PATH = project_path("agents", "buyer_intake_agent.md")

def load_agent_instructions():
    with open(AGENT_SPEC_PATH, "r", encoding="utf-8") as f:
        return f.read()

AGENT_INSTRUCTIONS = load_agent_instructions()
API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")

if not API_KEY:
    raise ValueError("No API key found in .env")

client = genai.Client(api_key=API_KEY)


# --------------------------------------------------
# CONFIG
# --------------------------------------------------

STATE_PATH = project_path("agents", "tests", "test_transaction_updated.json")
WORKFLOW_PATH = project_path("schemas", "intake_workflow.json")
SCHEMA_PATH = project_path("schemas", "transaction_schema.json")
APPLICATIONS_DIR = project_path("applications")
ACTIVE_APP_FILE = project_path("current_application.txt")

# --------------------------------------------------
# LOADERS
# --------------------------------------------------


def new_application_id():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    short = uuid.uuid4().hex[:4].upper()
    return f"{ts}-{short}"

def get_active_application_id():
    if not os.path.exists(ACTIVE_APP_FILE):
        return None
    with open(ACTIVE_APP_FILE, "r") as f:
        return f.read().strip()


def set_active_application_id(app_id):
    with open(ACTIVE_APP_FILE, "w") as f:
        f.write(app_id)


def application_file(app_id):
    return os.path.join(APPLICATIONS_DIR, f"{app_id}.json")

def load_json(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing file: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state() -> dict:

    if DEV_ALWAYS_NEW:
        os.makedirs(APPLICATIONS_DIR, exist_ok=True)

        app_id = new_application_id()
        set_active_application_id(app_id)

        state = load_json(SCHEMA_PATH)
        state["application_id"] = app_id
        state["workflow_runtime"] = {"active_index": 0}
        save_state(state)
        return state

    
    # ensure applications folder exists
    os.makedirs(APPLICATIONS_DIR, exist_ok=True)

    workflow = load_json(WORKFLOW_PATH)
    total_fields = sum(len(stage["fields"]) for stage in workflow["stages"])

    app_id = get_active_application_id()

    # ---------- NO ACTIVE APPLICATION ----------
    if not app_id:
        print("Creating new loan application...")
        app_id = new_application_id()
        set_active_application_id(app_id)

        state = load_json(SCHEMA_PATH)
        state["application_id"] = app_id
        state["workflow_runtime"] = {"active_index": 0}

        save_state(state)
        return state

    # ---------- LOAD EXISTING ----------
    path = application_file(app_id)

    if not os.path.exists(path):
        print("Application pointer invalid — starting new...")
        return load_state()

    state = load_json(path)

    # ---------- CHECK COMPLETED ----------
    active = state.get("workflow_runtime", {}).get("active_index", 0)

    if active >= total_fields:
        print("Previous application completed — starting a new one...\n")

        app_id = new_application_id()
        set_active_application_id(app_id)

        state = load_json(SCHEMA_PATH)
        state["application_id"] = app_id
        state["workflow_runtime"] = {"active_index": 0}

        save_state(state)
        return state

    return state


def save_state(state: dict):
    app_id = state["application_id"]
    path = application_file(app_id)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# --------------------------------------------------
# WORKFLOW INTERPRETER
# --------------------------------------------------

def flatten_fields(workflow: dict) -> List[Dict[str, Any]]:
    fields = []
    for stage in workflow.get("stages", []):
        for f in stage.get("fields", []):
            fields.append(f)
    return fields

def find_section_start(meta_fields, array_path):
    for i, f in enumerate(meta_fields):
        if f["path"].startswith(array_path + "[0]"):
            return i
    return None

# DECTECT END OF REPEATING SECTION
def is_last_field_of_section(meta_fields, idx, section_fields):
    if idx + 1 >= len(meta_fields):
        return True
    return meta_fields[idx + 1]["path"] not in section_fields

# --------------------------------------------------
# POINTER HELPERS
# --------------------------------------------------

def get_active_index(state: dict) -> int:
    return state.setdefault("workflow_runtime", {}).setdefault("active_index", 0)


def set_active_index(state: dict, idx: int):
    state.setdefault("workflow_runtime", {})["active_index"] = idx


def current_field(meta_fields: List[dict], state: dict) -> Optional[dict]:
    idx = get_active_index(state)
    if idx >= len(meta_fields):
        return None
    return meta_fields[idx]


def advance_field(state: dict):
    set_active_index(state, get_active_index(state) + 1)

def render_question(state, field_meta):
    question = field_meta.get("question", field_meta.get("path"))

    runtime = state.get("workflow_runtime", {})
    indices = runtime.get("array_index", {})

    path = resolve_dynamic_path(state, field_meta["path"])

    for array_path, idx in indices.items():
        if path.startswith(array_path + "[") and idx > 0:
            # specific rewrite for income
            if "annual income" in question.lower():
                return question.replace(
                    "your annual income",
                    "your additional annual income"
                )

            return "For this additional entry — " + question

    return question

# --------------------------------------------------
# PATH UTILITIES (supports a.b.c and list[0])
# --------------------------------------------------

def _parse_path(path: str):
    parts = []
    for token in path.split('.'):
        if '[' in token and token.endswith(']'):
            name, idx = token[:-1].split('[')
            parts.append(name)
            parts.append(int(idx))
        else:
            parts.append(token)
    return parts


def get_by_path(state: dict, path: str):
    ref = state
    try:
        for p in _parse_path(path):
            ref = ref[p]
        return ref
    except Exception:
        return None


def set_by_path(state: dict, path: str, value):
    ref = state
    parts = _parse_path(path)

    for i, p in enumerate(parts[:-1]):
        nxt = parts[i+1]

        if isinstance(p, int):
            # ensure list
            while len(ref) <= p:
                ref.append({})
            ref = ref[p]

        else:
            # decide dict vs list based on next token
            if isinstance(nxt, int):
                ref = ref.setdefault(p, [])
            else:
                ref = ref.setdefault(p, {})

    last = parts[-1]

    if isinstance(last, int):
        while len(ref) <= last:
            ref.append(None)
        ref[last] = value
    else:
        ref[last] = value

def resolve_dynamic_path(state, path):
    runtime = state.get("workflow_runtime", {})
    indices = runtime.get("array_index", {})

    for arr_path, idx in indices.items():
        path = path.replace(f"{arr_path}[0]", f"{arr_path}[{idx}]")

    return path


def get_array_parent(path: str):
    if "[" in path:
        return path.split("[")[0]
    return None

def workflow_stage_repeat_prompt(workflow: dict, array_path: str) -> str:
    for stage in workflow.get("stages", []):
        repeat = stage.get("section_repeat")
        if repeat and repeat.get("array_path") == array_path:
            return repeat.get("repeat_prompt")
    return "Do you have another Item?"

# --------------------------------------------------
# VALIDATION (presence only)
# --------------------------------------------------

def field_filled(state: dict, path: str) -> bool:
    path = resolve_dynamic_path(state, path)
    val = get_by_path(state, path)
    if val is None:
        return False
    if isinstance(val, str) and not val.strip():
        return False
    return True

# --------------------------------------------------
# ENUM NORMALIZATION
# --------------------------------------------------

def normalize_enum(value: str, allowed: list[str]):
    value = value.lower().strip()

    # remove punctuation
    value = re.sub(r"[^a-z ]", "", value)

    # exact match first
    for v in allowed:
        if value == v.replace("_", " "):
            return v

    # fuzzy match (more tolerant)
    matches = get_close_matches(
        value,
        [v.replace("_", " ") for v in allowed],
        n=1,
        cutoff=0.6   # was 0.75
    )

    if matches:
        matched = matches[0]
        for v in allowed:
            if matched == v.replace("_", " "):
                return v

    return None


# --------------------------------------------------
# DETERMINISTIC CAPTURE (minimal examples)
# --------------------------------------------------

def deterministic_capture(state: dict, field_meta: dict, user_text: str) -> bool:
    path = resolve_dynamic_path(state, field_meta["path"])
    raw = user_text.strip()
    text = raw.lower().replace(",", "").replace("$", "")

    # INTEGER
    if field_meta.get("type") == "integer" and text.isdigit():
        set_by_path(state, path, int(text))
        return True

    # CURRENCY (2000, 2k, 2.5k, $2000)
    # CURRENCY — only accept single number, reject ranges
    if field_meta.get("type") == "currency":

        # reject ranges like 70-80, 2 to 3, 5/6
        if re.search(r"\d+\s*[-/to]+\s*\d+", text):
            return False

        # reject vague qualifiers
        if any(w in text for w in ["about", "around", "rough", "approx", "maybe", "depends"]):
            return False

        m = re.fullmatch(r"\$?\s*(\d+(\.\d+)?)\s*k?", text)

        if not m:
            return False
        
        value = float(m.group(1))
        if "k" in text:
            value *= 1000
        set_by_path(state, path, int(value))
        return True


    # ENUM
    if field_meta.get("type") == "enum":
        normalized = normalize_enum(text, field_meta.get("values", []))
        if normalized:
            set_by_path(state, path, normalized)
            return True 

    # STRING
    if field_meta.get("type") == "string" and len(raw) > 0:
        set_by_path(state, path, raw)
        return True

    # DATE
    # DATE (DD/MM/YYYY or D/M/YYYY)
    if field_meta.get("type") == "date":
        m = re.fullmatch(r"(0?[1-9]|[12][0-9]|3[01])/(0?[1-9]|1[0-2])/\d{4}", text)
        if not m:
            return False

        set_by_path(state, path, text)
        return True

    return False


# --------------------------------------------------
# EXTRACTION AGENT (stub — wire LLM later)
# --------------------------------------------------

def extraction_agent(state: dict, field_meta: dict, user_text: str):

    target_path = resolve_dynamic_path(state, field_meta["path"])

    system_prompt = f"""
You are a regulated banking data extraction engine.

Target field:
{target_path}

Return a JSON array with ONE object.

PRIORITY RULES:

1) If the user indicates existence of an additional item in a repeating section,
   return add_object immediately — even if no value is given.

2) Only if no structural intent exists, attempt value extraction.

3) Never return "none" when structural intent is present.

Operations:
replace  → user gave one clear numeric value
uncertain → user mentioned numbers but they are ambiguous
none → user did not provide any number at all

IMPORTANT RULE:
If the message contains ANY numeric amount but it is not a single exact value,
you MUST return "uncertain" — NEVER "none".

Ambiguous examples:
- 70-80k
- about 5k
- around 5000
- 2 or 3 thousand
- depends
- maybe 4000

Examples:

"75000"
→ [{{"operation":"replace","path":"{target_path}","value":75000}}]

"roughly 70-80k"
→ [{{"operation":"uncertain","reason":"range_detected"}}]

"about 5000"
→ [{{"operation":"uncertain","reason":"approximate"}}]

"no income"
→ [{{"operation":"none"}}]

Return JSON only.

STRUCTURAL INTENT (VERY IMPORTANT)

When the user mentions:
- another job
- second job
- additional income
- more income sources
- side job
- freelance work alongside main job

You MUST return:

[{{"operation":"add_object","target_array":"compliance.financial_inquiry.income_sources"}}]

Do NOT ask for amount yet.
The system will handle questioning.


"""
# PASS ARRAY CONTEXT TO THE AGENT

    array_parent = get_array_parent(target_path)

    array_context = ""
    if array_parent:
        array_context = f"""
CURRENT SECTION:
This question belongs to a repeating collection:
{array_parent}

If the user mentions another item in this same category,
you MUST create a new object using add_object.
"""

    system_prompt = array_context + system_prompt

    prompt = f"""
{system_prompt}

{AGENT_INSTRUCTIONS}

CURRENT STATE:
{json.dumps(state, indent=2)}

USER MESSAGE:
{user_text}
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json"
            }
        )
        
        text = response.text

        # extract first JSON array
        start = text.find('[')
        end = text.rfind(']')
        if start == -1 or end == -1:
            return []

        patches = json.loads(text[start:end+1])

        # ---------- SAFETY FILTER ----------
        safe = []

        for p in patches:
            op = p.get("operation")

            # allow structure creation
            if op == "add_object":
                safe.append(p)
                continue

            # normal field update
            if p.get("path") == target_path:
                safe.append(p)

        return safe


    except Exception:
        return []

# --------------------------------------------------
# VALIDATION
# --------------------------------------------------

from datetime import datetime

def validate_value(field_meta, value):

    if value is None:
        return False, "missing"

    ftype = field_meta.get("type")

    # ---------- DATE ----------
    if ftype == "date":
        try:
            dt = datetime.strptime(value, "%d/%m/%Y")

            # sanity bounds
            if dt.year < 1900 or dt.year > datetime.now().year - 18:
                return False, "invalid_age"

        except Exception:
            return False, "invalid_format"

    # ---------- CURRENCY ----------
    if ftype == "currency":
        if not isinstance(value, (int, float)):
            return False, "not_numeric"

        if "min" in field_meta and value < field_meta["min"]:
            return False, "too_small"

        if "max" in field_meta and value > field_meta["max"]:
            return False, "too_large"

    return True, None



# --------------------------------------------------
# MAIN LOOP (FSM)
# --------------------------------------------------

def run():
    # map array_path → list of field paths
    need_prompt = True
    state = load_state()
    workflow = load_json(WORKFLOW_PATH)
    meta_fields = flatten_fields(workflow)

    section_map = {}
    for stage in workflow.get("stages", []):
        repeat = stage.get("section_repeat")
        if repeat:
            array_path = repeat["array_path"]
            section_map[array_path] = [f["path"] for f in stage["fields"]]

    print(f"--- {workflow.get('workflow_name','Loan Intake')} ---")

    while True:
        field_meta = current_field(meta_fields, state)
        if field_meta is None:
            print("Application complete.")
            break

        question = field_meta.get("question", field_meta.get("path"))
        if need_prompt:
            question = render_question(state, field_meta)
            print(f"Agent: {question}")

        user = input("You: ")
        need_prompt = True

        if user.lower() in ["exit", "quit"]:
            break

        last_field = state.get("workflow_runtime", {}).get("last_field")

        if last_field:
            # if user answer matches enum of previous field
            prev_meta = next((f for f in meta_fields if f["path"] == last_field), None)
            if prev_meta and prev_meta.get("type") == "enum":
                normalized = normalize_enum(user, prev_meta.get("values", []))
                if normalized:
                    set_by_path(state, last_field, normalized)
                    print("Agent: Got it — updated previous answer.")
                    # move pointer BACK to previous field so user sees correct flow
                    state["workflow_runtime"]["active_index"] -= 1
                    save_state(state)
                    continue

        runtime = state.setdefault("workflow_runtime", {})

        if runtime.get("awaiting_repeat_for"):
            answer = user.lower().strip()

            # YES → create another object
            if answer in ["yes", "y"]:
                array_path = runtime["awaiting_repeat_for"]

                arr = get_by_path(state, array_path) or []
                arr.append({})
                set_by_path(state, array_path, arr)

                runtime.setdefault("array_index", {})[array_path] = len(arr) - 1
                section_start = find_section_start(meta_fields, array_path)
                set_active_index(state, section_start)

                runtime.pop("awaiting_repeat_for")
                runtime.pop("repeat_prompt")
                save_state(state)
                need_prompt = True
                continue

            # NO → move forward
            elif answer in ["no", "n"]:
                runtime.pop("awaiting_repeat_for")
                runtime.pop("repeat_prompt")
                advance_field(state)
                save_state(state)
                need_prompt = True
                continue

            # INVALID → ask again
            else:
                print(f"Agent: Please answer yes or no — {runtime['repeat_prompt']}")
                need_prompt = False
                continue


        # STEP 1 — deterministic
        captured = deterministic_capture(state, field_meta, user)

        # STEP 2 — LLM extraction
        if not captured:
            patches = extraction_agent(state, field_meta, user)
            handled = False

            for p in patches:
                op = p.get("operation")

                if op == "uncertain":
                    print(f"Agent: I want to be precise — {field_meta['question']}")
                    handled = True
                    break

                if op == "replace":
                    resolved_path = resolve_dynamic_path(state, field_meta["path"])
                    set_by_path(state, resolved_path, p.get("value"))
                    handled = True
                    break

                if op == "add_object":
                    array_path = p.get("target_array")

                    arr = get_by_path(state, array_path)
                    if not isinstance(arr, list):
                        arr = []
                        set_by_path(state, array_path, arr)

                    arr.append({})  # create empty structured object

                    runtime = state.setdefault("workflow_runtime", {})
                    runtime.setdefault("array_index", {})
                    runtime["array_index"][array_path] = len(arr) - 1

                    section_start = find_section_start(meta_fields, array_path)
                    if section_start is not None:
                        set_active_index(state, section_start)

                    print("Agent: Got it — let's capture the additional details.")

                    handled = True
                    need_prompt = True
                    break

            if handled:
                continue
            runtime = state.setdefault("workflow_runtime", {})
            retries = runtime.get("clarify_retries", 0)

            if not handled:
                if re.search(r"\d", user):
                    if retries >= 2:
                        print("Agent: Let's enter a precise number to continue.")
                    else:
                        print(f"Agent: I want to be precise — {field_meta['question']}")
                    runtime["clarify_retries"] = retries + 1
                    need_prompt = False
                    continue

            runtime["clarify_retries"] = 0



        # STEP 3 — validate
        if field_filled(state, field_meta["path"]):

            resolved_path = resolve_dynamic_path(state, field_meta["path"])
            value = get_by_path(state, resolved_path)
            ok, reason = validate_value(field_meta, value)

            if ok:
                runtime = state.setdefault("workflow_runtime", {})
                active_idx = get_active_index(state)

                # check if field belongs to a repeating section
                for array_path, fields in section_map.items():
                    if field_meta["path"] in fields:
                        if is_last_field_of_section(meta_fields, active_idx, fields):
                            runtime["awaiting_repeat_for"] = array_path
                            runtime["repeat_prompt"] = workflow_stage_repeat_prompt(workflow, array_path)
                            save_state(state)
                            print(f"Agent: {runtime['repeat_prompt']}")
                            need_prompt = False
                            continue

                advance_field(state)
                runtime["last_field"] = field_meta["path"]
                save_state(state)
                continue


            # reject value
            set_by_path(state, resolved_path, None)
            print(f"Agent: That value seems unusual ({reason}). {field_meta['question']}")
            need_prompt = False
            continue

if __name__ == "__main__":
    import sys

    if "--new" in sys.argv:
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)
        print("Starting a fresh application...\n")

    run()

