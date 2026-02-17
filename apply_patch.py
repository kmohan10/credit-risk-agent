import json
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def get_by_path(root, path):
    """Retrieve a value from a nested dict using a dot-separated path."""
    if not path:
        return root
    
    parts = path.split('.')
    current = root
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                # If the part is an index (e.g., "0"), use it
                idx = int(part)
                current = current[idx]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current

def set_by_path(root, path, value, operation="replace"):
    """
    Set or modify a value in a nested dict using a dot-separated path.
    Supported operations: add, replace, append
    """
    if not path:
        return False
    
    parts = path.split('.')
    current = root
    
    # Navigate to the parent of the target field
    for i in range(len(parts) - 1):
        part = parts[i]
        
        if part not in current:
            # If path doesn't exist, create missing dicts (for 'add' or 'append' operations)
            current[part] = {}
        
        current = current[part]
        if not isinstance(current, dict):
            logger.error(f"Cannot navigate path '{path}': '{part}' is not a dictionary.")
            return False

    target_key = parts[-1]

    if operation == "add":
        # 'add' creates a new entry; if it exists, it behaves like replace
        current[target_key] = value
    elif operation == "replace":
        # 'replace' updates an existing entry
        current[target_key] = value
    elif operation == "append":
        # 'append' assumes the target is a list
        if target_key not in current:
            current[target_key] = []
        
        if isinstance(current[target_key], list):
            current[target_key].append(value)
        else:
            logger.error(f"Cannot append to '{path}': target is not a list.")
            return False
    elif operation == "none":
        # Do nothing
        pass
    else:
        logger.warning(f"Unknown operation: {operation}")
        return False

    return True

def set_nested_value(data, path, value, op="replace"):
    keys = path.split(".")
    current = data

    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]

    final_key = keys[-1]

    if op == "append":
        if final_key not in current or not isinstance(current[final_key], list):
            current[final_key] = []
        current[final_key].append(value)
    else:  # add or replace
        current[final_key] = value

def apply_patches(state, patches):
   
    results = []
    for patch in patches:
        op = patch.get("operation")
        path = patch.get("path")
        val = patch.get("value")
        
        # FIX: normalize path
        path = patch.get("path","")
        if path.startswith("/"):
            path = path.lstrip("/").replace("/", ".")
            patch["path"] = path

        # Prevent agent from advancing workflow stage prematurely
        if path == "workflow_flags.expense_primer_shown" and val is True:
            # Only allow if last ask_user message was the primer
            last_message = patch.get("justification", "")
            if "primer" not in last_message.lower():
                logger.warning("Blocked premature primer flag set")
        results.append({"status": "blocked", "patch": patch})
        continue

        if op == "none":
            results.append({"status": "ignored", "patch": patch})
            continue

        set_nested_value(state, path, val, op)
        success = True

        results.append({
            "status": "success",
            "patch": patch
        })

        logger.info(f"Applied {op} to {path} (Justification: {patch.get('justification')})")
            
    return results
