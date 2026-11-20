def patch(text,edits):
    for start,end,value in sorted(edits,reverse=True):
        text=text[:start]+value+text[end:]
    return text
