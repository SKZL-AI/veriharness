# Spec: greet() takes a name

## Goal

`greet.py` exposes one function, `greet(name: str) -> str`, that returns
`"Hello, <name>!"`. Right now the file does not exist. This is deliberately
small: the point of this example is to show the HoH loop working end to end
in a few minutes, not to exercise it on a hard problem.

## Scope -- what you may create and change

- `greet.py`
- `test_greet.py`

## Out of scope

- Everything else in this directory.

## Acceptance criteria

1. **K1 -- `greet.py` exists and defines `greet`.**
   Command: `python3 -c "from greet import greet; assert callable(greet)"`

2. **K2 -- `greet("World")` returns exactly `"Hello, World!"`.**
   Command: `python3 -c "from greet import greet; assert greet('World') == 'Hello, World!'"`

3. **K3 -- a test file exists and passes.**
   Command: `python3 -m pytest -q test_greet.py`

Criteria 1 to 3 are red on the starting state: neither `greet.py` nor
`test_greet.py` exists yet.
