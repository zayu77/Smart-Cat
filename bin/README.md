# bin

板端一键执行入口。

```text
run_smart_scale.sh         单次完整交易：同步配置/策略、拍照、识别、称重、计价、播报、记录、上报
run_smart_scale_service.sh 常驻称重触发服务：空秤待机、重量稳定后自动交易
run_voice_command.sh       本地语音补盲命令：状态、重量、最近交易、价格、待确认
run_voice_command_mqtt.sh  MQTT 语音补盲监听：接收 Web/手机下发命令并播报
```

在板端项目根目录执行：

```bash
chmod +x bin/*.sh
./bin/run_smart_scale.sh
```
