# constants.py

PYTHON_KEYWORDS = {
    'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await',
    'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except',
    'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is',
    'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try',
    'while', 'with', 'yield'
}

SUPPORTED_FIELD_TYPES = {
    'CharField', 'TextField', 'BooleanField', 'DateField',
    'DateTimeField', 'IntegerField', 'DecimalField', 'EmailField'
}

SYSTEM_RESERVED_FIELD_NAMES = {'id', 'create_time', 'update_time'}