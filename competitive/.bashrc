alias bo='/home/soyuz/competitive/sh/bo.sh'

cp() {
    if [ "$#" -eq 0 ] || { [ "$#" -eq 1 ] && [ "$1" = "-a" ]; }; then
        /home/soyuz/competitive/sh/cp.sh "$@"
    else
        /usr/bin/cp "$@"
    fi
}

init() {
    /home/soyuz/competitive/sh/init.sh
}

gen() {
    python3 generator.py > in.txt
}

contest() {
    /home/soyuz/competitive/sh/contest.sh "$@"
}

sub() {
    /home/soyuz/competitive/sh/sub.sh "$@"
}

atcoder-login() {
    /home/soyuz/competitive/.venv/bin/python /home/soyuz/competitive/sh/atcoder_login.py
}
