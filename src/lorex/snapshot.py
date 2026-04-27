"""ffmpeg RTSP -> JPEG snapshot helper.

Spawns ffmpeg via asyncio.create_subprocess_exec so the snapshot doesn't
block the plugin worker's event loop. Returns the JPEG bytes.
"""
import asyncio


async def take_snapshot_via_ffmpeg(ffmpeg_path: str, rtsp_url: str,
                                    timeout_sec: float = 15.0) -> bytes:
    args = [
        '-rtsp_transport', 'tcp',
        '-i', rtsp_url,
        '-frames:v', '1',
        '-q:v', '2',
        '-f', 'image2',
        '-loglevel', 'error',
        'pipe:1',
    ]
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise Exception(f'ffmpeg snapshot timed out after {timeout_sec}s')

    if proc.returncode != 0:
        raise Exception(
            f'ffmpeg snapshot failed (exit={proc.returncode}): '
            f'{err.decode("utf-8", errors="replace")[:400]}')
    return out
