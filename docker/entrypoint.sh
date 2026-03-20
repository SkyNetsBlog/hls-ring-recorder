#!/bin/bash
POLL_INTERVAL=${POLL_INTERVAL:-15}
BLANK_TIMEOUT=${BLANK_TIMEOUT:-60}  # stop recording after this many seconds of black screen
SEGMENT_TIME=${SEGMENT_TIME:-200}   # duration of each segment in seconds
SEGMENT_WRAP=${SEGMENT_WRAP:-400}   # number of segments in the ring buffer (%03d zero-pads names up to 999; beyond that filenames grow to 4+ digits but still work)
LUMA_THRESHOLD=${LUMA_THRESHOLD:-15} # minimum luma (0-255) to consider a frame non-black

[ -n "${HLS_STREAM:-}" ] || { echo "HLS_STREAM is not set"; exit 1; }

HLS_TMP=/data/.tmp
mkdir -p "$HLS_TMP"

# Heartbeat for Kubernetes liveness probe
while true; do date +%s > /tmp/heartbeat; sleep 30; done &
HEARTBEAT_PID=$!

atomize_segments() {
    # Recovery only runs on first startup, not on inotifywait crash-restarts.
    # /tmp is ephemeral so the sentinel clears on pod restart.
    if [ ! -f /tmp/.atomize_initialized ]; then
        touch /tmp/.atomize_initialized
        local max_n=-1
        for f in "$HLS_TMP"/*.ts; do
            [ -f "$f" ] || continue
            local name; name=$(basename "$f")
            mv "$f" "/data/$name"
            echo "Recovered partial segment from previous run: $name"
            local n=${name#segment_}; n=${n%.ts}
            [ $(( 10#$n )) -gt "$max_n" ] && max_n=$(( 10#$n ))
        done
        [ "$max_n" -ge 0 ] && echo $(( (max_n + 1) % SEGMENT_WRAP )) > /data/.next_segment
    fi

    inotifywait -m -e close_write --format '%f' "$HLS_TMP" 2>/dev/null \
    | while read -r filename; do
        [[ "$filename" == *.ts ]] || continue
        mv "$HLS_TMP/$filename" "/data/$filename"
        local n=${filename#segment_}; n=${n%.ts}
        echo $(( (10#$n + 1) % SEGMENT_WRAP )) > /data/.next_segment
    done
}
while true; do atomize_segments; echo "atomize_segments exited unexpectedly, restarting..."; sleep 1; done &
ATOMIZE_PID=$!

notify_on_segment() {
    [ -z "${WEBHOOK_URL:-}" ] && return
    echo "Webhook notifications enabled -> $WEBHOOK_URL"
    inotifywait -m -e moved_to --format '%f' /data 2>/dev/null \
    | while read -r filename; do
        [[ "$filename" == *.ts ]] || continue
        (
            payload=$(jq -n --arg segment "$filename" --arg path "/data/$filename" \
                '{"segment":$segment,"path":$path}')
            status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 -X POST "$WEBHOOK_URL" \
                -H "Content-Type: application/json" \
                -d "$payload")
            [[ "$status" == 2* ]] || echo "Webhook POST failed for $filename: HTTP $status"
        ) &
    done
}

NOTIFY_PID=
if [ -n "${WEBHOOK_URL:-}" ]; then
    while true; do notify_on_segment; echo "notify_on_segment exited unexpectedly, restarting..."; sleep 1; done &
    NOTIFY_PID=$!
fi

# Returns true if the stream's current frame is non-black (luma > $LUMA_THRESHOLD out of 255).
# Used to avoid recording black screens — the stream is often live but showing nothing.
stream_is_nonblack() {
    local luma
    luma=$(ffmpeg -loglevel error -timeout 10000000 -i "$HLS_STREAM" \
        -frames:v 1 -vf "scale=1:1,format=gray" \
        -f rawvideo - 2>/dev/null \
        | od -An -tu1 | awk 'NR==1{print $1+0}')
    [ "${luma:-0}" -gt "$LUMA_THRESHOLD" ]
}

wait_for_picture() {
    echo "Waiting for non-black picture on stream at $HLS_STREAM..."
    local poll=0
    until stream_is_nonblack; do
        sleep "$POLL_INTERVAL"
        poll=$(( poll + 1 ))
        echo "  Still waiting... (${poll} poll(s), $(( poll * POLL_INTERVAL ))s elapsed)"
    done
    echo "Picture detected, starting recording."
}

monitor_for_blank() {
    local ffmpeg_pid=$1 luma_file=$2 blank_since=0
    while kill -0 "$ffmpeg_pid" 2>/dev/null; do
        sleep "$POLL_INTERVAL"
        local luma
        luma=$(tail -c 1 "$luma_file" 2>/dev/null | od -An -tu1 | awk 'NR==1{print $1+0}')
        # Default to non-black until first keyframe is written
        if [ "${luma:-$((LUMA_THRESHOLD + 1))}" -gt "$LUMA_THRESHOLD" ]; then
            if [ "$blank_since" -ne 0 ]; then
                echo "Content resumed."
                blank_since=0
            fi
        else
            if [ "$blank_since" -eq 0 ]; then
                blank_since=$(date +%s)
                echo "Black screen detected, blank timer started."
            else
                local elapsed
                elapsed=$(( $(date +%s) - blank_since ))
                echo "Stream black for ${elapsed}s / ${BLANK_TIMEOUT}s."
                if [ "$elapsed" -ge "$BLANK_TIMEOUT" ]; then
                    echo "Blank timeout reached, stopping recording."
                    kill "$ffmpeg_pid"
                    return
                fi
            fi
        fi
    done
}

next_segment_number() {
    cat /data/.next_segment 2>/dev/null || echo 0
}

PROGRESS_FILE=
LUMA_FILE=
trap 'kill "$FFMPEG_PID" "$MONITOR_PID" ${NOTIFY_PID:+"$NOTIFY_PID"} "$ATOMIZE_PID" "$HEARTBEAT_PID" 2>/dev/null; rm -f "$PROGRESS_FILE" "$LUMA_FILE"; exit' TERM INT

while true; do
    wait_for_picture

    START_NUM=$(next_segment_number)
    PROGRESS_FILE=$(mktemp)
    LUMA_FILE=$(mktemp)
    ffmpeg -y -reconnect 1 -reconnect_delay_max 5 -skip_frame nokey -i "$HLS_STREAM" \
        -c copy \
        -f segment \
        -segment_time "$SEGMENT_TIME" \
        -segment_wrap "$SEGMENT_WRAP" \
        -segment_start_number "$START_NUM" \
        -segment_format mpegts \
        -progress "$PROGRESS_FILE" \
        -nostats \
        "$HLS_TMP/segment_%03d.ts" \
        -map 0:v:0 \
        -vf "scale=1:1,format=gray" -pix_fmt gray -an \
        -f rawvideo "$LUMA_FILE" &
    FFMPEG_PID=$!

    monitor_for_blank "$FFMPEG_PID" "$LUMA_FILE" &
    MONITOR_PID=$!

    while kill -0 "$FFMPEG_PID" 2>/dev/null; do
        t=$(tail -n 20 "$PROGRESS_FILE" 2>/dev/null | grep "^out_time=" | tail -1 | cut -d= -f2)
        s=$(tail -n 20 "$PROGRESS_FILE" 2>/dev/null | grep "^total_size=" | tail -1 | cut -d= -f2)
        printf "\r  Recording: %s  %.1f MB  " "${t:--}" "$(awk "BEGIN{printf \"%.1f\", ${s:-0}/1048576}")"
        sleep 5
    done

    kill "$MONITOR_PID" 2>/dev/null
    wait "$MONITOR_PID" "$FFMPEG_PID" 2>/dev/null
    rm -f "$PROGRESS_FILE" "$LUMA_FILE"

    echo
    echo "Stream ended or blank. Polling..."
    sleep "$POLL_INTERVAL"
done
