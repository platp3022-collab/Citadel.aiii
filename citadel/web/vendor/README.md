# Сторонние файлы

## lightweight-charts.standalone.production.js

Библиотека графиков [Lightweight Charts](https://github.com/tradingview/lightweight-charts)
от TradingView, версия 4.2.3, лицензия Apache 2.0 (текст — в
`LICENSE-lightweight-charts.txt`).

Это тот же движок, на котором сделаны графики на самом TradingView и на многих
крипто-сайтах: свечи, курсор с ценой и временем, зум колесом, прокрутка мышью,
ценовая шкала справа, шкала времени снизу.

Файл лежит в репозитории целиком, чтобы панель работала офлайн и не тянула
ничего из интернета. Обновляется вручную:

```bash
npm pack lightweight-charts@4
tar xzf lightweight-charts-*.tgz
cp package/dist/lightweight-charts.standalone.production.js citadel/web/vendor/
cp package/LICENSE citadel/web/vendor/LICENSE-lightweight-charts.txt
```
