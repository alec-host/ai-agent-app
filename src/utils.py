# src/utils.py

def sanitize_history(history: list, max_content_length: int = 1000):
    """
    Truncates the 'content' field of messages in history to save tokens.
    """
    sanitized = []
    for msg in history:
        # We want to keep the structure but trim the fat
        msg_dict = msg.dict(exclude_none=True) if hasattr(msg, 'dict') else msg
        
        if "content" in msg_dict and msg_dict["content"]:
            if len(msg_dict["content"]) > max_content_length:
                msg_dict["content"] = msg_dict["content"][:max_content_length] + "... [Content Truncated]"
        
        sanitized.append(msg_dict)
    return sanitized