import json,sys,app
sys.stdout.write(app.render(json.load(sys.stdin)))
