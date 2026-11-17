# AWS Emulator を動かすためのイメージ。
# 公式ドキュメントの Dockerfile を、RPM の種類（CPU）を選べるようにしたもの。
# ./emu build が、Apple Silicon なら aarch64、Intel なら x86_64 を渡す。
# https://docs.aws.amazon.com/emulator/latest/userguide/getting-started-docker.html
FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal
ARG RPM_ARCH=aarch64
ADD https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/latest/aws-emulator-${RPM_ARCH}.rpm /tmp/aws-emulator.rpm
RUN rpm -i /tmp/aws-emulator.rpm && rm /tmp/aws-emulator.rpm \
    && mkdir -p /home/emulator && chown 1000 /home/emulator
USER 1000
ENV HOME=/home/emulator
EXPOSE 9297
ENTRYPOINT ["aws-emulator", "-b", "0.0.0.0"]
