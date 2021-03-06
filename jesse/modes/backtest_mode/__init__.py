import time

import arrow
import click
import numpy as np

import jesse.helpers as jh
import jesse.services.selectors as selectors
import jesse.services.statistics as stats
import jesse.services.table as table
from jesse.config import config
from jesse.enums import timeframes
from jesse import exceptions
from jesse.models import Candle
from jesse.routes import router
from jesse.services import charts
from jesse.services import report
from jesse.services.cache import cache
from jesse.services.candle import generate_candle_from_one_minutes, print_candle, candle_includes_price, split_candle
from jesse.services.file import store_logs
from jesse.store import store
import jesse.services.required_candles as required_candles
from jesse.services.validators import validate_routes


def run(start_date: str, finish_date: str, candles=None, chart=False, tradingview=False):
    # clear the screen
    if not jh.should_execute_silently():
        click.clear()

    # validate routes
    validate_routes(router)

    # initiate candle store
    store.candles.init_storage(5000)

    # load historical candles
    if candles is None:
        print('loading candles...')
        candles = _load_candles(start_date, finish_date)
        click.clear()

    if not jh.should_execute_silently():
        # print candles table
        key = '{}-{}'.format(config['app']['trading_exchanges'][0], config['app']['trading_symbols'][0])
        table.key_value(stats.candles(candles[key]['candles']), 'candles', alignments=('left', 'right'))
        print('\n')

        # print routes table
        table.multi_value(stats.routes(router.routes))
        print('\n')

        # print guidance for debugging candles
        if jh.is_debuggable('trading_candles') or jh.is_debuggable('shorter_period_candles'):
            print('     Symbol  |     timestamp    | open | close | high | low | volume')

    # run backtest simulation
    simulator(candles)

    if not jh.should_execute_silently():
        # print trades statistics
        if store.completed_trades.count > 0:
            print('\n')
            table.key_value(report.portfolio_metrics(), 'Metrics', alignments=('left', 'right'))
            print('\n')

            # save logs
            store_logs(tradingview)

            if chart:
                charts.portfolio_vs_asset_returns()
        else:
            print(jh.color('No trades were made.', 'yellow'))


def _load_candles(start_date_str: str, finish_date_str: str):
    start_date = jh.arrow_to_timestamp(arrow.get(start_date_str, 'YYYY-MM-DD'))
    finish_date = jh.arrow_to_timestamp(arrow.get(finish_date_str, 'YYYY-MM-DD')) - 60000

    # validate
    if start_date == finish_date:
        raise ValueError('start_date and finish_date cannot be the same.')
    if start_date > finish_date:
        raise ValueError('start_date cannot be bigger than finish_date.')
    if finish_date > arrow.utcnow().timestamp * 1000:
        raise ValueError('Can\'t backtest the future!')

    # load and add required initial candles for backtest
    for c in config['app']['considering_candles']:
        required_candles.inject_required_candles_to_store(
            required_candles.load_required_candles(c[0], c[1], start_date_str, finish_date_str),
            c[0],
            c[1]
        )

    # download candles for the duration of the backtest
    candles = {}
    for exchange in config['app']['considering_exchanges']:
        for symbol in config['app']['considering_symbols']:
            key = jh.key(exchange, symbol)

            cache_key = '{}-{}-'.format(start_date_str, finish_date_str) + key
            cached_value = cache.get_value(cache_key)
            # if cache exists
            if cached_value:
                candles_tuple = cached_value
            # not cached, get and cache for later calls in the next 5 minutes
            else:
                # fetch from database
                candles_tuple = Candle.select(
                    Candle.timestamp, Candle.open, Candle.close, Candle.high, Candle.low,
                    Candle.volume
                ).where(
                    Candle.timestamp.between(start_date, finish_date),
                    Candle.exchange == exchange,
                    Candle.symbol == symbol
                ).order_by(Candle.timestamp.asc()).tuples()

            # validate that there are enough candles for selected period
            required_candles_count = (finish_date - start_date) / 60_000
            if len(candles_tuple) == 0 or candles_tuple[-1][0] != finish_date or candles_tuple[0][0] != start_date:
                raise exceptions.CandleNotFoundInDatabase('Not enough candles for {}. Try running "jesse import-candles"'.format(symbol))
            elif len(candles_tuple) != required_candles_count + 1:
                raise exceptions.CandleNotFoundInDatabase('There are missing candles between {} => {}'.format(
                    start_date_str, finish_date_str
                ))

            # cache it for near future calls
            cache.set_value(cache_key, tuple(candles_tuple), expire_seconds=60*60*24*7)

            candles[key] = {
                'exchange': exchange,
                'symbol': symbol,
                'candles': np.array(candles_tuple)
            }

    return candles


def simulator(candles, hyper_parameters=None):
    begin_time_track = time.time()
    key = '{}-{}'.format(config['app']['trading_exchanges'][0], config['app']['trading_symbols'][0])
    first_candles_set = candles[key]['candles']
    length = len(first_candles_set)
    # to preset the array size for performance
    store.app.starting_time = first_candles_set[0][0]

    # initiate strategies
    for r in router.routes:
        StrategyClass = jh.get_strategy_class(r.strategy_name)

        # convert DNS string into hyper_parameters
        if r.dna and hyper_parameters is None:
            hyper_parameters = jh.dna_to_hp(StrategyClass.hyper_parameters(), r.dna)

        r.strategy = StrategyClass()
        r.strategy.name = r.strategy_name
        r.strategy.exchange = r.exchange
        r.strategy.symbol = r.symbol
        r.strategy.timeframe = r.timeframe

        # init few objects that couldn't be initiated in Strategy __init__
        r.strategy._init_objects()

        # inject hyper parameters (used for optimize_mode)
        if hyper_parameters is not None:
            r.strategy.hp = hyper_parameters

        selectors.get_position(r.exchange, r.symbol).strategy = r.strategy

    # add initial balance
    _save_daily_portfolio_balance()

    with click.progressbar(length=length, label='Executing simulation...') as progressbar:
        for i in range(length):
            # update time
            store.app.time = first_candles_set[i][0] + 60_000

            # add candles
            for j in candles:
                short_candle = candles[j]['candles'][i]
                exchange = candles[j]['exchange']
                symbol = candles[j]['symbol']

                store.candles.add_candle(short_candle, exchange, symbol, '1m', with_execution=False,
                                         with_generation=False)

                # print short candle
                if jh.is_debuggable('shorter_period_candles'):
                    print_candle(short_candle, True, symbol)

                _simulate_price_change_effect(short_candle, exchange, symbol)

                # generate and add candles for bigger timeframes
                for timeframe in config['app']['considering_timeframes']:
                    # for 1m, no work is needed
                    if timeframe == '1m':
                        continue

                    count = jh.timeframe_to_one_minutes(timeframe)
                    until = count - ((i + 1) % count)

                    if (i + 1) % count == 0:
                        generated_candle = generate_candle_from_one_minutes(
                            timeframe,
                            candles[j]['candles'][(i - (count - 1)):(i + 1)])
                        store.candles.add_candle(generated_candle, exchange, symbol, timeframe, with_execution=False,
                                                 with_generation=False)

            # update progressbar
            if not jh.is_debugging() and not jh.should_execute_silently() and i % 60 == 0:
                progressbar.update(60)

            # now that all new generated candles are ready, execute
            for r in router.routes:
                count = jh.timeframe_to_one_minutes(r.timeframe)
                # 1m timeframe
                if r.timeframe == timeframes.MINUTE_1:
                    r.strategy._execute()
                elif (i + 1) % count == 0:
                    # print candle
                    if jh.is_debuggable('trading_candles'):
                        print_candle(store.candles.get_current_candle(r.exchange, r.symbol, r.timeframe), False,
                                     r.symbol)
                    r.strategy._execute()

            # now check to see if there's any MARKET orders waiting to be executed
            store.orders.execute_pending_market_orders()

            if i != 0 and i % 1440 == 0:
                _save_daily_portfolio_balance()

    if not jh.should_execute_silently():
        if jh.is_debuggable('trading_candles') or jh.is_debuggable('shorter_period_candles'):
            print('\n')

        # print executed time for the backtest session
        finish_time_track = time.time()
        print('Executed backtest simulation in: ', '{} seconds'.format(round(finish_time_track - begin_time_track, 2)))

    for r in router.routes:
        r.strategy._terminate()

    # now that backtest is finished, add finishing balance
    _save_daily_portfolio_balance()


def _save_daily_portfolio_balance():
    balances = []

    # add exchange balances
    for key, e in store.exchanges.storage.items():
        balances.append(e.balance)

    # add open position values
    for key, pos in store.positions.storage.items():
        if pos.is_open:
            balances.append(pos.value)
        else:
            # if position is close, see if we have active orders for that route
            for o in store.orders.get_orders(pos.exchange_name, pos.symbol):
                if o.is_active:
                    balances.append(abs(o.qty * o.price))

    store.app.daily_balance.append(sum(balances))


def _simulate_price_change_effect(real_candle: np.ndarray, exchange: str, symbol: str):
    orders = store.orders.get_orders(exchange, symbol)

    current_temp_candle = real_candle.copy()
    executed_order = False

    while True:
        if len(orders) == 0:
            executed_order = False
        else:
            for index, order in enumerate(orders):
                if index == len(orders) - 1 and not order.is_active:
                    executed_order = False

                if not order.is_active:
                    continue

                if candle_includes_price(current_temp_candle, order.price):
                    storable_temp_candle, current_temp_candle = split_candle(current_temp_candle, order.price)
                    store.candles.add_candle(
                        storable_temp_candle, exchange, symbol, '1m',
                        with_execution=False,
                        with_generation=False
                    )
                    p = selectors.get_position(exchange, symbol)
                    p.current_price = storable_temp_candle[2]

                    executed_order = True

                    order.execute()

                    # break from the for loop, we'll try again inside the while
                    # loop with the new current_temp_candle
                    break
                else:
                    executed_order = False

        if not executed_order:
            # add/update the real_candle to the store so we can move on
            store.candles.add_candle(
                real_candle, exchange, symbol, '1m',
                with_execution=False,
                with_generation=False
            )
            p = selectors.get_position(exchange, symbol)
            p.current_price = real_candle[2]
            break
