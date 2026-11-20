import app
def timeout(text=None):
    return app.parse(text or '1s')
