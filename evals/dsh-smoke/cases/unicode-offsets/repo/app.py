def replace_spans(text, spans):
    for start,end,replacement in spans:
        text = text[:start] + replacement + text[end:]
    return text
