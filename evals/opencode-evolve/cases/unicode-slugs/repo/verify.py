from app import slug
assert slug('  Hello, 世界! ') == 'hello-世界'
