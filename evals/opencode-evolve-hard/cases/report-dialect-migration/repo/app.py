def render(rows):
    return 'name,count\n'+''.join(f'{name},{count}\n' for name,count in rows)
