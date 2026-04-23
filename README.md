# SoloX - Real-time Performance Monitoring for Android & iOS

<p align="center">
  <a>English</a> | <a href="./README.zh.md">中文</a> | <a href="./FAQ.md">FAQ</a> | <a href="https://mp.weixin.qq.com/s?__biz=MzkxMzYyNDM2NA==&mid=2247484506&idx=1&sn=b7eb6de68f84bed03001375d08e08ce9&chksm=c17b9819f60c110fd14e652c104237821b95a13da04618e98d2cf27afa798cb45e53cf50f5bd&token=1402046775&lang=zh_CN&poc_token=HKmRi2WjP7gf9CVwvLWQ2cRhrUR3wmbB9-fNZdD4" target="__blank">使用文档</a>
</p>

<p align="center">
<a href="#">
<img src="https://cdn.nlark.com/yuque/0/2024/png/153412/1715927541315-fb4f7662-d8bb-4d3e-a712-13a3c3073ac8.png?x-oss-process=image%2Fformat%2Cwebp" alt="SoloX" width="100">
</a>
<br>
</p>

<p align="center">
<a href="https://pypi.org/project/solox/" target="__blank"><img src="https://img.shields.io/pypi/v/solox" alt="solox preview"></a>
<a href="https://pepy.tech/project/solox" target="__blank"><img src="https://static.pepy.tech/personalized-badge/solox?period=total&units=international_system&left_color=grey&right_color=orange&left_text=downloads"></a>
<a href="https://github.com/psf/black"><img src="https://img.shields.io/badge/code%20style-black-000000.svg" alt="Black"></a>
<a href="https://github.com/Minions777/SoloX/stargazers"><img src="https://img.shields.io/github/stars/Minions777/SoloX?style=flat-square" alt="Stars"></a>

<br>
</p>

## 🔎Preview

SoloX - Real-time collection tool for Android/iOS performance data.

Quickly locate and analyze performance issues to improve application performance and quality. **No ROOT/jailbreak required** - plug and play.

![SoloX Preview](https://github.com/smart-test-ti/SoloX/assets/24454096/5b33183c-dcf3-48b7-8c91-dfe20bff3d5c)

## ✨ What's New

| Version | Features |
|---------|----------|
| v2.10.0 | 🌟 **Android 16 & iOS 26 Support** - Latest OS compatibility with gfxinfo framestats fallback |
| v2.9.0 | 🎨 **Modern UI** - Redesigned interface with enhanced visual design |
| v2.8.5 | 📊 **Python API** - Full performance monitoring API support |
| v2.1.5 | 🔗 **Service API** - HTTP API for remote monitoring |

## 📦 Requirements

| Component | Requirement |
|------------|-------------|
| Python | 3.10+ |
| Android | 6.0+ (tested up to **Android 16**) |
| iOS | py-ios-device support (**iOS 17+**), taobao-iphone-device (iOS 10-16) |
| ADB | Required for Android (configure PATH) |

### iOS Specific
- **iOS 17+**: Requires `py-ios-device` (automatically used)
- **iOS 10-16**: Requires iTunes on Windows, native support on macOS

## 📥 Installation

### Stable Release
```shell
pip install -U solox
```

### Specific Version
```shell
pip install solox==2.10.0
```

### China Mirror
```shell
pip install -i https://mirrors.ustc.edu.cn/pypi/web/simple -U solox
```

## 🚀 Quick Start

### Basic Usage
```shell
python -m solox
```

### Custom Server
```shell
python -m solox --host=0.0.0.0 --port=6006
```

## 🐍 Python API

```python
from solox.public.apm import AppPerformanceMonitor
from solox.public.common import Devices

# Get device process list (Android)
d = Devices()
processList = d.getPid(deviceId='ca6bd5a5', pkgName='com.bilibili.app.in')
print(processList)  # ['{pid}:{packagename}', ...]

# Initialize monitor
apm = AppPerformanceMonitor(
    pkgName='com.bilibili.app.in',
    platform='Android',
    deviceId='ca6bd5a5',
    surfaceview=True,  # Use SurfaceFlinger (not gfxinfo)
    noLog=False,       # Save test data to log file
    record=False,      # Record screen during test
    collect_all=False
)

# Collect single metric
cpu = apm.collectCpu()           # CPU usage (%)
memory = apm.collectMemory()     # Memory (MB)
fps = apm.collectFps()           # Frames per second (Hz)
gpu = apm.collectGpu()           # GPU usage (%)
network = apm.collectNetwork()   # Network traffic (KB)
battery = apm.collectBattery()  # Battery level, temp, current

# Collect all metrics with report
if __name__ == '__main__':
    apm = AppPerformanceMonitor(
        pkgName='com.bilibili.app.in',
        platform='Android',
        deviceId='ca6bd5a5',
        collect_all=True,
        duration=300  # 5 minutes
    )
    apm.collectAll(report_path='/path/to/report.html')
```

### iOS Example
```python
from solox.public.apm import AppPerformanceMonitor

apm = AppPerformanceMonitor(
    pkgName='com.bilibili.app.in',
    platform='iOS',
    deviceId='xxxx',  # iOS device ID
    noLog=False,
    collect_all=True
)
apm.collectAll()
```

## 🔗 Service API

### Start as Background Service
```shell
# macOS/Linux
nohup python3 -m solox &

# Windows
start /min python3 -m solox
```

### Query Performance Data
```shell
# Android
curl "http://localhost:6006/apm/collect?platform=Android&deviceid=DEVICE_ID&pkgname=com.example.app&target=cpu"

# iOS
curl "http://localhost:6006/apm/collect?platform=iOS&pkgname=com.example.app&target=cpu"

# Available targets: cpu, memory, memory_detail, network, fps, battery, gpu
```

## 🔥 Features

| Feature | Description |
|---------|-------------|
| 📱 **No ROOT Required** | Android 6.0+ without ROOT, iOS without Jailbreak |
| 📊 **Comprehensive Metrics** | CPU, GPU, Memory, Network, FPS, Jank, Battery |
| 📈 **Beautiful Reports** | Interactive charts, data export, compare mode |
| ⚡ **Real-time Monitoring** | Live performance data with low overhead |
| 🔄 **PK Mode** | Compare two devices or two apps simultaneously |
| 🐍 **Python API** | Integrate into your automation framework |
| 🌐 **Remote Access** | Monitor devices on other machines |
| 🔔 **Alert Settings** | Configurable thresholds for all metrics |
| ⏰ **Scheduled Tests** | Set duration and automatic collection |

### Supported Platforms

| Platform | Version | Notes |
|----------|---------|-------|
| Android | 6.0 - 16+ | Uses gfxinfo for SDK 31+ (Android 12+) |
| iOS | 10 - 16 | taobao-iphone-device |
| iOS | 17+ | py-ios-device |

## 🖥️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      SoloX Web UI                          │
│                    (Tabler Dashboard)                       │
├─────────────────────────────────────────────────────────────┤
│                      Flask Server                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │   APM    │  │   View   │  │  WebSocket │ │   API   │   │
│  │  Module  │  │  Module  │  │  Real-time │ │ Service │   │
│  └────┬─────┘  └────┬─────┘  └─────┬─────┘  └────┬─────┘   │
├───────┼─────────────┼──────────────┼─────────────┼────────┤
│       │             │              │             │        │
│  ┌────▼─────┐  ┌────▼─────┐  ┌─────▼─────┐  ┌───▼───┐   │
│  │   ADB    │  │py-ios-   │  │  Web      │  │ File  │   │
│  │ (Android)│  │ device   │  │  Socket   │  │ Store │   │
│  └──────────┘  │  (iOS)   │  └───────────┘  └────────┘   │
│                └──────────┘                                  │
└─────────────────────────────────────────────────────────────┘
```

## 🛠️ Development

### Tech Stack
- **Web Framework**: [Flask](https://github.com/pallets/flask)
- **UI Library**: [Tabler](https://github.com/tabler/tabler)
- **Charts**: ApexCharts, Highcharts
- **Mobile Access**: ADB, [py-ios-device](https://github.com/Minions777/py-ios-device), [taobao-iphone-device](https://github.com/alibaba/taobao-iphone-device)

### Debug Mode
```shell
# Remove [solox] module prefix from imports
# Example: from solox.view.apis import api  →  from view.apis import api

cd solox
python debug.py
```

## 📋 Report Metrics

| Metric | Description | Android | iOS |
|--------|-------------|---------|-----|
| CPU App | Application CPU usage | ✅ | ✅ |
| CPU System | System CPU usage | ✅ | - |
| Memory | Total memory usage | ✅ | ✅ |
| Memory Detail | Detailed memory breakdown | ✅ | - |
| FPS | Frames per second | ✅ | ✅ |
| Jank | Frame drops | ✅ | - |
| GPU | GPU usage | ✅ | - |
| Network Up | Upload traffic | ✅ | ✅ |
| Network Down | Download traffic | ✅ | ✅ |
| Battery | Battery level & status | ✅ | - |

## 🎨 Screenshots

### Normal Mode
Performance monitoring for single device

### PK Mode
Compare two devices or apps simultaneously
- **2-devices**: Same app on two different phones
- **2-apps**: Different apps on phones with same configuration

## 💕 Acknowledgements

- [taobao-iphone-device](https://github.com/alibaba/taobao-iphone-device) - iOS device access
- [py-ios-device](https://github.com/Minions777/py-ios-device) - iOS 17+ support
- [scrcpy](https://github.com/Genymobile/scrcpy) - Android screen mirroring
- [Tabler](https://github.com/tabler/tabler) - Dashboard UI framework

## 📄 License

MIT License - See [LICENSE](LICENSE) for details.

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

---

<p align="center">
Made with ❤️ by <a href="https://github.com/smart-test-ti">SMART TEST</a> Team
</p>
