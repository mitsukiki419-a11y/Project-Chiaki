# The Android ffmpeg

Android stops ordinary Linux programs built with glibc (they use system calls Android's app sandbox forbids:
they are killed, exit code 159). The ffmpeg for the Android app is therefore built **statically with musl**,
with only what Project Chiaki needs (decoders, demuxers, MJPEG/PNG/PCM output, a few filters). No GPL parts are
enabled, so it is under the **LGPL 2.1**.

- Source: FFmpeg 6.1.1, https://ffmpeg.org/releases/ffmpeg-6.1.1.tar.xz
- C library: musl 1.2.4 (https://musl.libc.org); zlib 1.3

Configure (aarch64; for x86_64 use `--arch=x86_64 --disable-x86asm` and an x86_64 musl-gcc):

```
./configure --enable-cross-compile --arch=aarch64 --target-os=linux --cc=<musl-gcc> --cross-prefix=aarch64-linux-gnu- \
  --disable-everything --disable-autodetect --disable-network --disable-doc --disable-ffplay --disable-ffprobe \
  --disable-debug --enable-static --disable-shared --extra-ldflags=-static --pkg-config=false \
  --enable-decoders --enable-demuxers --enable-parsers --enable-protocol='file,pipe' \
  --enable-encoder='mjpeg,png,pcm_s16le' --enable-muxer='mjpeg,image2,image2pipe,s16le,wav,null' \
  --enable-filter='yadif,fps,scale,realtime,volume,aresample,aformat,format,null,anull,color,setpts,trim' \
  --enable-indev=lavfi --enable-swscale --enable-swresample --enable-avfilter --enable-zlib
make
```
