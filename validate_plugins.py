# validate_plugins.py

import os
import importlib.util

PLUGIN_DIR = "plugins"

def load_plugins():
    plugins = {}
    for filename in os.listdir(PLUGIN_DIR):
        if filename.endswith(".py") and filename != "__init__.py":
            plugin_path = os.path.join(PLUGIN_DIR, filename)
            module_name = filename[:-3]
            spec = importlib.util.spec_from_file_location(module_name, plugin_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            if hasattr(mod, "description") and hasattr(mod, "run_scraper"):
                plugins[module_name] = mod
    return plugins

