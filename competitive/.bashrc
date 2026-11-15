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
