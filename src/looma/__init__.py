import os

__version__ = "0.1.0"

# Looma was called DevDash: settings made under that name (DEVDASH_HOME and the rest) still
# count, under their new LOOMA_ names, unless the new one is set too.
for _name, _value in list(os.environ.items()):
    if _name.startswith("DEVDASH_"):
        os.environ.setdefault("LOOMA_" + _name[len("DEVDASH_"):], _value)
