# Retiring the File Browser container (step 14)

Phoenix's File browser page (sidebar → File browser) replaces the File Browser container
(`filebrowser`, 2.63.23, upstream archived 2026-09-01, no security fixes). **Do not run these until
0.5.0 is installed (step 16) and you have used the new page for a day.** Nothing here has been run.

## 1. On the VPS, as root

```bash
docker stop filebrowser
docker rm filebrowser filebrowser-old
rm -rf /opt/filebrowser
docker ps -a --filter name=filebrowser          # expect: no rows
ss -ltnp | grep ':8080' || echo "8080 is free"
```

`docker rm` removes both the running container and the stopped rollback copy `filebrowser-old`.
`/opt/filebrowser` holds its database and settings. The image can be removed later with
`docker image ls filebrowser/filebrowser` and `docker rmi <id>` once nothing uses it.

## 2. On your Mac

Remove the 8080 forward from `~/.ssh/config`, in the `Host` block you use for `ssh root`. Delete only
the line that forwards 8080, and keep the 8090 line (the Phoenix dashboard):

```
    LocalForward 8080 127.0.0.1:8080     # remove this line
    LocalForward 8090 127.0.0.1:8090     # keep
```

Then open a new `ssh root` session. `http://127.0.0.1:8080` should no longer load, and the File browser
works at `http://127.0.0.1:8090/#/files`.

## Rollback (before step 1 only)

Until the containers are removed, the old File Browser still runs at 127.0.0.1:8080. After step 1
there is no rollback: the new page is the only file browser.
