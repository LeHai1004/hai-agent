from .web import web_search
from .time_tool import get_current_time
from .files import list_files, read_file, write_file
from .word import create_word
from .repo import read_repo, draw_diagram

TOOLS = [web_search, get_current_time, list_files, read_file, write_file, create_word, read_repo, draw_diagram]