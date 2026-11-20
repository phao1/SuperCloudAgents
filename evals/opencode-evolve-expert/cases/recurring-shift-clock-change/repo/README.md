# Crewcall

Crewcall plans local staffing for rehearsal halls that share a pool
of part-time crew. Desk operators define venue lanes and weekly
shift rules, make one-off schedule changes, assign staff, record
breaks and export calendar or payroll previews. It is a local tool
for Python 3.12 with a JSON desk file and no installation step.

    python -B -m crewcall --desk crew.json venue put stage Stage Europe/Berlin
    python -B -m crewcall --desk crew.json lane put door stage usher "Front door"
    python -B -m crewcall --desk crew.json rule put early door mon,wed 19:00 120 2024-10-01
    python -B -m crewcall --desk crew.json roster show 2024-10-01 2024-10-31

Start with [desk commands](docs/desk.md) and [planning](docs/planning.md).
The [operator exchange guide](docs/exports.md) documents export formats.
Accepted ADRs under docs/adr/ take precedence over older examples.
Run the desk regressions with python -B -m unittest discover -s tests.
