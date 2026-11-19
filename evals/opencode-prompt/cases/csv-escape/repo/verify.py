from app import render_row

assert render_row(["alpha,beta", "gamma"]) == '"alpha,beta",gamma\r\n'
