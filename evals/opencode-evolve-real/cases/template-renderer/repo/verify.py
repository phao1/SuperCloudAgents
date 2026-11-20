from app import render

assert render("{{#each rows as row}}[{{row}}]{{/each}}", {"rows": [1, 2]}) == "[1][2]"
