import os
import pwd
import sys
from pathlib import Path


data_directory = Path(os.getenv("DATA_DIR", "/app/data"))
app_user = pwd.getpwnam("app")
data_directory.mkdir(parents=True, exist_ok=True)
if os.geteuid() == 0:
    os.chown(data_directory, app_user.pw_uid, app_user.pw_gid)
    os.setgroups([])
    os.setgid(app_user.pw_gid)
    os.setuid(app_user.pw_uid)
os.execvp(sys.argv[1], sys.argv[1:])
