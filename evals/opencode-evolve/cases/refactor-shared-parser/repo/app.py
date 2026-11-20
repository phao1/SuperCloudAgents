def total(text):
    return sum(int(x.strip()) for x in text.split(',') if x.strip())
def largest(text):
    values = [int(x.strip()) for x in text.split(',') if x.strip()]
    return max(values) if values else None
