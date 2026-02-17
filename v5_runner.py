import os
import json
import logging
import shutil
import re
import google.generativeai as genai
import google.api_core.exceptions
from dotenv import load_dotenv
from apply_patch import apply_patches

# ---------------- CONFIG ----------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("ANTIGRAVITY_API_KEY")
if not API_KEY:
    logger.error("API Key not found. Please set GOOGLE_API_KEY or GEMINI_API_KEY.")
    exit(1)

genai.configure(api_key=API_KEY)

SCHEMA_PATH = "transaction_schema.json"
AGENT_MD_PATH = "agents/buyer_intake_agent.md"
INITIAL_STATE_PATH = "agents/tests/test_transaction.json"
UPDATED_STATE_PATH = "agents/tests/test_transaction_updated.json"


# ---------------- FILE HELPERS ----------------
def load_text(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)


# ---------------- RESET STATE ----------------
def reset_state():
    if not os.path.exists(INITIAL_STATE_PATH):
        raise FileNotFoundError(f"Missing base state: {INITIAL_STATE_PATH}")
    if os.path.exists(UPDATED_STATE_PATH):
        os.remove(UPDATED_STATE_PATH)
    shutil.copy(INITIAL_STATE_PATH, UPDATED_STATE_PATH)

FIELD_QUESTION_MAP = {
    "parties.buyer.name": "What is your full legal name as it appears on your identification?",
    "parties.buyer.dob": "What is your date of birth? (DD/MM/YYYY)",
    "parties.buyer.dependents": "How many dependents do you financially support?",
    "income": "What is your employment income before tax?",
    "expense_primer": "Think about a normal month. I will ask about a few common spending areas.",
    "expense:housing": "What is your monthly housing expense including rent or mortgage?",
    "expense:food": "What is your monthly food expense?",
    "expense:transport": "What is your monthly transport expense?",
    "expense:utilities": "What is your monthly utilities expense?",
    "expense:insurance": "What is your monthly insurance expense?",
    "expense:childcare": "What is your monthly childcare expense?",
    "expense:medical": "What is your monthly medical expense?",
    "expense:subscriptions": "What is your monthly subscriptions expense?",
    "expense:discretionary": "What is your monthly discretionary spending?"
}

# ---------------- ORCHESTRATOR ----------------
def resolve_next_field(state):
    buyer = state.get("parties", {}).get("buyer", {})
    fi = state.get("compliance", {}).get("financial_inquiry", {})
    expenses = fi.get("living_expenses", {})
    flags = state.get("workflow_flags", {})
    runtime = state.get("workflow_runtime", {})
    asked = runtime.get("asked_fields", [])

    # ---------------- CONVERSATIONAL IDENTITY RESOLUTION ----------------
    identity_order = ["parties.buyer.name", "parties.buyer.dob"]

    # First: ask missing identity fields once
    for field in identity_order:
        key = field.split('.')[-1]
        if not buyer.get(key) and field not in asked:
            return field

    # Second: if identity still incomplete but already asked → do NOT block flow
    # (this prevents repetition freeze)
    # --------------------------------------------------------------------

    if "dependents" not in buyer:
        return "parties.buyer.dependents"

    if not fi.get("income_sources"):
        return "income"

    if not flags.get("expense_primer_shown"):
        return "expense_primer"

    order = [
        "housing","food","transport","utilities",
        "insurance","childcare","medical","subscriptions","discretionary"
    ]

    for k in order:
        if k not in expenses:
            return f"expense:{k}"

    return "done"

# ---------------- AGENT TURN ----------------

def run_agent_turn(model, state, user_input, agent_instructions, target_field):

    system_control = f"""
You are a banking data extraction engine.

Your job:
Extract structured values for the specified field.

FIELD TO EXTRACT:
{target_field}

Rules:
- Return ONLY a JSON array
- Do NOT speak
- Do NOT ask questions
- Do NOT explain
- If no value found → return operation "none"
"""

    prompt = f"""
{system_control}

{agent_instructions}

CURRENT STATE:
{json.dumps(state, indent=2)}

USER INPUT:
{user_input}

Return JSON patches only.
"""

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()

        # Extract the FIRST valid JSON array (robust)
        start = text.find('[')
        end = text.rfind(']')
        if start == -1 or end == -1:
            logger.error("No JSON array detected")
            return []

        json_text = text[start:end+1]

        try:
            return json.loads(json_text)
        except Exception as e:
            logger.error(f"JSON parse failure: {e}")
            logger.error(f"Extracted text: {json_text}")
            return []

    except google.api_core.exceptions.ResourceExhausted:
        logger.error("Quota exceeded (429). Wait and retry.")
        return []

    except Exception as e:
        logger.error(f"Agent turn error: {e}")
        return []


#  Parser Function 

def deterministic_pre_extract(state, user_input):

    text = user_input.lower()
    buyer = state.setdefault("parties", {}).setdefault("buyer", {})
    fi = state.setdefault("compliance", {}).setdefault("financial_inquiry", {})

    # ---------------- DEPENDENTS ----------------
    if "dependents" not in buyer:
        if re.search(r'\b(no|zero|none|nil)\s+dependents\b', text) or \
           re.search(r"do not have.*dependents|don't have.*dependents", text):
            buyer["dependents"] = 0
            logger.info("Deterministic extraction: dependents = 0")

        m = re.search(r'(\d+)\s+dependents?', text)
        if m:
            buyer["dependents"] = int(m.group(1))
            logger.info(f"Deterministic extraction: dependents = {buyer['dependents']}")

    # ---------------- INCOME ----------------
    if not fi.get("income_sources"):
        m = re.search(r'(earn|make|salary|income)[^\d]{0,10}(\d{2,7})', text)
        if m:
            income = int(m.group(2))
            fi["income_sources"] = [{
                "type": "employment",
                "amount": income,
                "frequency": "annual",
                "employment_status": "unknown",
                "stability": "unknown",
                "verified": False,
                "verification_method": ""
            }]
            logger.info(f"Deterministic extraction: income = {income}")

    # ---------------- DOB ----------------
    if not buyer.get("dob"):
        m = re.search(r'\b(\d{1,2}/\d{1,2}/\d{2,4})\b', user_input)
        if m:
            dob = m.group(1)
            buyer["dob"] = dob
            logger.info(f"Deterministic extraction: dob = {dob}")

    # ---------------- NAME ----------------
    if not buyer.get("name"):
        # avoid capturing financial phrases as names
        name_match = re.search(r'\b([A-Z][a-z]{2,} [A-Z][a-z]{2,})\b', user_input)
        if name_match and not re.search(r'(loan|income|salary|credit|account)', user_input, re.I):
            buyer["name"] = name_match.group(1)
            logger.info(f"Deterministic extraction: name = {buyer['name']}")


# Field Capture Layer

def deterministic_field_capture(state, user_input):

    runtime = state.setdefault("workflow_runtime", {})
    last_field = runtime.get("last_question_field")

    if not last_field:
        return False

    buyer = state.setdefault("parties", {}).setdefault("buyer", {})
    text = user_input.strip()
    lower = text.lower()

    # ---- NAME ----
    if last_field == "parties.buyer.name":
        if len(text.split()) >= 2 and not any(x in lower for x in ["earn","salary","income","dependents"]):
            if not buyer.get("name"):
                buyer["name"] = text
                logger.info(f"Deterministic extraction: name = {text}")
                return True

    # ---- DOB ----
    elif last_field == "parties.buyer.dob":
        m = re.search(r'\b(\d{1,2}/\d{1,2}/\d{2,4})\b', text)
        if m:
            dob = m.group(1)
            if not buyer.get("dob"):
                buyer["dob"] = dob
                logger.info(f"Deterministic extraction: dob = {dob}")
                return True

    # ---- DEPENDENTS ----
    elif last_field == "parties.buyer.dependents":
        if lower in ["0","none","no","no dependents"]:
            buyer["dependents"] = 0
            logger.info("Deterministic extraction: dependents = 0")
            return True
        if text.isdigit():
            buyer["dependents"] = int(text)
            logger.info(f"Deterministic extraction: dependents = {text}")
            return True

    # ---- INCOME ----
    elif last_field == "income":
        m = re.search(r'\d+', text.replace(',', ''))
        if m:
            income = int(m.group())
            fi = state.setdefault("compliance", {}).setdefault("financial_inquiry", {})
            fi["income_sources"] = [{
                "type": "employment",
                "amount": income,
                "frequency": "annual",
                "employment_status": "unknown",
                "stability": "unknown",
                "verified": False,
                "verification_method": ""
            }]
            logger.info(f"Deterministic extraction: income = {income}")
            return True

    # ---- EXPENSE FIELDS ----
    elif last_field and last_field.startswith("expense:"):
        expense_type = last_field.split(":")[1]

        m = re.search(r'\d+', text.replace(',', ''))
        if m:
            amount = int(m.group())

            fi = state.setdefault("compliance", {}).setdefault("financial_inquiry", {})
            expenses = fi.setdefault("living_expenses", {})

            # only set if changed (prevents duplicate writes)
            if expenses.get(expense_type) != amount:
                expenses[expense_type] = amount
                logger.info(f"Deterministic extraction: expense {expense_type} = {amount}")

            return True

    return False



#---------------- MAIN LOOP ----------------

def main():
    logger.info("Starting v5 Agentic Test Harness")
    reset_state()

    agent_instructions = load_text(AGENT_MD_PATH)
    current_state = load_json(UPDATED_STATE_PATH)

    model = genai.GenerativeModel('models/gemini-2.0-flash')

    print("\n--- Credit Risk v5 Agent Session ---")
    print("Agent: Buyer Intake Agent")
    print("Type 'exit' to quit.\n")

    # ---------------- BOOTSTRAP FIRST QUESTION ----------------
    runtime = current_state.setdefault("workflow_runtime", {})
    runtime.setdefault("asked_fields", [])

    first_field = resolve_next_field(current_state)
    first_question = FIELD_QUESTION_MAP.get(first_field)

    runtime["last_question_field"] = first_field
    runtime["asked_fields"].append(first_field)

    save_json(UPDATED_STATE_PATH, current_state)

    print(f"Agent: {first_question}")


    # ---------------- CONVERSATION LOOP ----------------
    while True:
        user_input = input("You: ")
        if user_input.lower() in ['exit', 'quit', 'q']:
            break


        # STEP 0 - forward capture (user volunteers info)
        deterministic_pre_extract(current_state, user_input)
        save_json(UPDATED_STATE_PATH, current_state)

        # STEP 1 — capture answer to LAST question
        captured = deterministic_field_capture(current_state, user_input)
        if captured:
            save_json(UPDATED_STATE_PATH, current_state)

        # STEP 2 — determine what we need next
        target_field = resolve_next_field(current_state)

        # STEP 3 — call LLM only for complex extraction
        patches = run_agent_turn(
            model,
            current_state,
            user_input,
            agent_instructions,
            target_field
        )

        # STEP 4 — apply patches
        real_patches = [p for p in patches if p.get("operation") != "none"]
        if real_patches:
            apply_patches(current_state, real_patches)
            save_json(UPDATED_STATE_PATH, current_state)

        # STEP 5 — decide what to ask NEXT (safe conversational resolver)

        while True:

            next_field = resolve_next_field(current_state)

            if next_field == "done":
                print("Agent: Application complete")
                return

            question = FIELD_QUESTION_MAP.get(next_field)

            runtime = current_state.setdefault("workflow_runtime", {})
            runtime.setdefault("asked_fields", [])

            # speak only if not asked before
            if next_field not in runtime["asked_fields"]:
                runtime["asked_fields"].append(next_field)
                runtime["last_question_field"] = next_field
                save_json(UPDATED_STATE_PATH, current_state)
                print(f"Agent: {question}")
                
                # mark system prompts as completed
                if next_field == "expense_primer":
                    current_state.setdefault("workflow_flags", {})["expense_primer_shown"] = True
                    save_json(UPDATED_STATE_PATH, current_state)

                break   


            # otherwise skip and find next askable field
            runtime["last_question_field"] = next_field



if __name__ == "__main__":
    main()

