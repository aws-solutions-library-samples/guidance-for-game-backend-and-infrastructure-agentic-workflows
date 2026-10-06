# GameLift-ready runtime image for the migration target.
# Binaries are cross-compiled by build.sh (GameLift SDK wrapper from the
# Containers Starter Kit + the echo game server). Runs as a non-root user on
# Amazon Linux 2023, matching the container fleet's AMAZON_LINUX_2023 OS.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023
RUN dnf install -y -q procps-ng shadow-utils && dnf clean all \
 && groupadd --gid 1000 serveruser \
 && useradd --uid 1000 --gid serveruser -m serveruser
USER serveruser
WORKDIR /home/serveruser
COPY --chown=serveruser:serveruser bin/gameliftwrapper bin/echoserver wrapper.sh ./
RUN chmod +x ./wrapper.sh ./gameliftwrapper ./echoserver
ENV GAME_PORT=7654
CMD ["./wrapper.sh"]
