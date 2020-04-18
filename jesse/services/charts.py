from datetime import datetime, timedelta
import arrow

import numpy as np
import pandas as pd
from matplotlib import pyplot as plt
from pandas.plotting import register_matplotlib_converters

from jesse.store import store
from jesse.routes import router
from jesse.config import config
import jesse.helpers as jh


def portfolio_vs_asset_returns():
    register_matplotlib_converters()
    trades = store.completed_trades.trades
    # create a plot figure
    plt.figure(figsize=(26, 16))

    # daily balance
    plt.subplot(2, 1, 1)
    start_date = datetime.fromtimestamp(store.app.starting_time / 1000)
    date_list = [start_date + timedelta(days=x) for x in range(len(store.app.daily_balance))]
    plt.xlabel('date')
    plt.ylabel('balance')
    plt.title('Portfolio Daily Return')
    plt.plot(date_list, store.app.daily_balance)

    # price change%
    plt.subplot(2, 1, 2)
    price_dict = {}
    for r in router.routes:
        key = jh.key(r.exchange, r.symbol)
        price_dict[key] = {
            'indexes': {},
            'prices': []
        }
        dates = []
        prices = []
        candles = store.candles.get_candles(r.exchange, r.symbol, '1m')
        max_timeframe = jh.max_timeframe(config['app']['considering_timeframes'])
        pre_candles_count = jh.timeframe_to_one_minutes(max_timeframe) * 210
        for i, c in enumerate(candles):
            # do not plot prices for required_initial_candles period
            if i < pre_candles_count:
                continue

            dates.append(datetime.fromtimestamp(c[0] / 1000))
            prices.append(c[2])
            # save index of the price instead of the actual price
            price_dict[key]['indexes'][str(int(c[0]))] = len(prices)-1

        # price => %returns
        price_returns = pd.Series(prices).pct_change(1) * 100
        cumsum_returns = np.cumsum(price_returns)
        if len(router.routes) == 1:
            plt.plot(dates, cumsum_returns, label=r.symbol, c='grey')
        else:
            plt.plot(dates, cumsum_returns, label=r.symbol)
        price_dict[key]['prices'] = cumsum_returns

    # buy and sell plots
    buy_x = []
    buy_y = []
    sell_x = []
    sell_y = []
    for t in trades:
        key = jh.key(t.exchange, t.symbol)

        if t.type == 'long':
            buy_x.append(datetime.fromtimestamp(t.opened_at / 1000))
            sell_x.append(datetime.fromtimestamp(t.closed_at / 1000))
            # add price change%
            buy_y.append(
                price_dict[key]['prices'][price_dict[key]['indexes'][str(int(t.opened_at))]]
            )
            sell_y.append(
                price_dict[key]['prices'][price_dict[key]['indexes'][str(int(t.closed_at))]]
            )
        elif t.type == 'short':
            buy_x.append(datetime.fromtimestamp(t.closed_at / 1000))
            sell_x.append(datetime.fromtimestamp(t.opened_at / 1000))
            # add price change%
            buy_y.append(
                price_dict[key]['prices'][price_dict[key]['indexes'][str(int(t.closed_at))]]
            )
            sell_y.append(
                price_dict[key]['prices'][price_dict[key]['indexes'][str(int(t.opened_at))]]
            )

    plt.plot(buy_x, buy_y, '.', color='green')
    plt.plot(sell_x, sell_y, '.', color='red')

    plt.xlabel('date')
    plt.ylabel('price change %')
    plt.title('Asset Daily Return')
    plt.legend(loc='upper left')

    # store final result
    mode = config['app']['trading_mode']
    if mode == 'backtest':
        mode = 'BT'
    if mode == 'livetrade':
        mode = 'LT'
    if mode == 'papertrade':
        mode = 'PT'
    file_path = 'storage/charts/{}-{}.png'.format(
        mode, str(arrow.utcnow())[0:19]
    ).replace(":", "-")
    plt.savefig(file_path)
    print(
        'Chart output saved at:\n{}'.format(file_path)
    )