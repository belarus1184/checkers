import os
import uuid
from flask import Flask, render_template, request, session
from flask_socketio import SocketIO, emit, join_room, leave_room
import json

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret!')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Хранилище игр: room_id -> game_state
games = {}

# ---------- Игровая логика (международные шашки 10x10) ----------
BOARD_SIZE = 10
VALUE_MAN = 1
VALUE_KING = 5

def create_initial_board():
    board = [[None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]
    # Чёрные (сверху) на тёмных клетках (row+col)%2 == 1
    for row in range(4):
        for col in range(BOARD_SIZE):
            if (row + col) % 2 == 1:
                board[row][col] = {'type': 'man', 'color': 'black'}
    # Белые (снизу)
    for row in range(6, 10):
        for col in range(BOARD_SIZE):
            if (row + col) % 2 == 1:
                board[row][col] = {'type': 'man', 'color': 'white'}
    return board

def copy_board(board):
    return [[cell.copy() if cell else None for cell in row] for row in board]

def is_valid_cell(row, col):
    return 0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE and (row + col) % 2 == 1

# ----- Ходы простой шашки -----
def get_man_moves(board, row, col, piece):
    moves = []
    forward_dirs = [(-1, -1), (-1, 1)] if piece['color'] == 'white' else [(1, -1), (1, 1)]
    for dr, dc in forward_dirs:
        nr, nc = row + dr, col + dc
        if is_valid_cell(nr, nc) and board[nr][nc] is None:
            moves.append({'from': (row, col), 'to': (nr, nc), 'captures': []})
    return moves

def get_man_captures(board, row, col, piece, captured=None):
    if captured is None:
        captured = []
    captures = []
    dirs = [(-1,-1), (-1,1), (1,-1), (1,1)]  # все направления для боя (включая назад)
    for dr, dc in dirs:
        mid_r, mid_c = row + dr, col + dc
        land_r, land_c = row + 2*dr, col + 2*dc
        if (is_valid_cell(land_r, land_c) and board[land_r][land_c] is None and
            is_valid_cell(mid_r, mid_c) and board[mid_r][mid_c] is not None and
            board[mid_r][mid_c]['color'] != piece['color']):
            # Выполняем взятие
            new_board = copy_board(board)
            jumped = new_board[mid_r][mid_c]
            new_board[mid_r][mid_c] = None
            new_board[land_r][land_c] = piece.copy()
            new_board[row][col] = None
            # Превращение в дамку
            if piece['type'] == 'man' and (land_r == 0 or land_r == 9):
                new_board[land_r][land_c]['type'] = 'king'
            new_captured = captured + [jumped]
            # Продолжаем бой той же шашкой
            next_captures = get_captures_for_piece(new_board, land_r, land_c, new_board[land_r][land_c], new_captured)
            if not next_captures:
                captures.append({
                    'from': (row, col),
                    'to': (land_r, land_c),
                    'captures': new_captured,
                    'board_after': new_board
                })
            else:
                for nc in next_captures:
                    captures.append({
                        'from': (row, col),
                        'to': nc['to'],
                        'captures': new_captured + nc['captures'],
                        'board_after': nc['board_after']
                    })
    return captures

# ----- Ходы дамки (на любое расстояние) -----
def get_king_moves(board, row, col, piece):
    moves = []
    dirs = [(-1,-1), (-1,1), (1,-1), (1,1)]
    for dr, dc in dirs:
        nr, nc = row + dr, col + dc
        while is_valid_cell(nr, nc) and board[nr][nc] is None:
            moves.append({'from': (row, col), 'to': (nr, nc), 'captures': []})
            nr += dr
            nc += dc
    return moves

def get_king_captures(board, row, col, piece, captured=None):
    if captured is None:
        captured = []
    captures = []
    dirs = [(-1,-1), (-1,1), (1,-1), (1,1)]
    for dr, dc in dirs:
        # Ищем первую шашку противника на луче
        r, c = row + dr, col + dc
        while is_valid_cell(r, c) and board[r][c] is None:
            r += dr
            c += dc
        if is_valid_cell(r, c) and board[r][c] is not None and board[r][c]['color'] != piece['color']:
            jumped_piece = board[r][c]
            # Пытаемся прыгнуть на любое пустое поле за шашкой
            land_r, land_c = r + dr, c + dc
            while is_valid_cell(land_r, land_c) and board[land_r][land_c] is None:
                new_board = copy_board(board)
                new_board[r][c] = None
                new_board[land_r][land_c] = piece.copy()
                new_board[row][col] = None
                new_captured = captured + [jumped_piece]
                # После прыжка дамка может продолжить бой
                next_captures = get_captures_for_piece(new_board, land_r, land_c, new_board[land_r][land_c], new_captured)
                if not next_captures:
                    captures.append({
                        'from': (row, col),
                        'to': (land_r, land_c),
                        'captures': new_captured,
                        'board_after': new_board
                    })
                else:
                    for nc in next_captures:
                        captures.append({
                            'from': (row, col),
                            'to': nc['to'],
                            'captures': new_captured + nc['captures'],
                            'board_after': nc['board_after']
                        })
                land_r += dr
                land_c += dc
    return captures

def get_moves_for_piece(board, row, col, piece):
    if piece['type'] == 'man':
        return get_man_moves(board, row, col, piece)
    else:
        return get_king_moves(board, row, col, piece)

def get_captures_for_piece(board, row, col, piece, captured=None):
    if piece['type'] == 'man':
        return get_man_captures(board, row, col, piece, captured)
    else:
        return get_king_captures(board, row, col, piece, captured)

def get_all_moves(board, color):
    all_moves = []
    max_captures = 0
    # Собираем все взятия
    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            piece = board[r][c]
            if piece and piece['color'] == color:
                caps = get_captures_for_piece(board, r, c, piece)
                for cap in caps:
                    cnt = len(cap['captures'])
                    if cnt > max_captures:
                        max_captures = cnt
                    all_moves.append(cap)
    if max_captures > 0:
        return [m for m in all_moves if len(m['captures']) == max_captures]
    # Нет взятий – тихие ходы
    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            piece = board[r][c]
            if piece and piece['color'] == color:
                all_moves.extend(get_moves_for_piece(board, r, c, piece))
    return all_moves

def apply_move(board, move):
    if 'board_after' in move:
        return move['board_after']
    new_board = copy_board(board)
    piece = new_board[move['from'][0]][move['from'][1]]
    new_board[move['from'][0]][move['from'][1]] = None
    new_board[move['to'][0]][move['to'][1]] = piece
    if piece['type'] == 'man' and (move['to'][0] == 0 or move['to'][0] == 9):
        new_board[move['to'][0]][move['to'][1]]['type'] = 'king'
    return new_board

def has_any_move(board, color):
    return len(get_all_moves(board, color)) > 0

def evaluate_board(board):
    white = 0
    black = 0
    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            piece = board[r][c]
            if piece:
                val = VALUE_KING if piece['type'] == 'king' else VALUE_MAN
                # Бонус за центр (только для простых)
                if piece['type'] == 'man':
                    center_dist = abs(r - 4.5) + abs(c - 4.5)
                    center_bonus = (9 - center_dist) * 0.05
                    val += center_bonus
                if piece['color'] == 'white':
                    white += val
                else:
                    black += val
    return white - black

def create_game_state():
    return {
        'board': create_initial_board(),
        'current_turn': 'white',
        'winner': None,
        'players': {'white': None, 'black': None},  # socket_id
        'names': {'white': None, 'black': None},
        'evaluation_history': [evaluate_board(create_initial_board())]
    }

# ---------- Flask routes ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/game/<room_id>')
def game(room_id):
    if room_id not in games:
        return "Игра не найдена", 404
    return render_template('game.html', room_id=room_id)

# ---------- Socket.IO events ----------
@socketio.on('create_game')
def handle_create_game(data):
    name = data['name']
    color = data['color']  # 'white' или 'black'
    room_id = str(uuid.uuid4())[:8]
    game = create_game_state()
    game['players'][color] = request.sid
    game['names'][color] = name
    games[room_id] = game
    join_room(room_id)
    emit('game_created', {'room_id': room_id, 'color': color}, room=request.sid)

@socketio.on('join_game')
def handle_join_game(data):
    room_id = data['room_id']
    name = data['name']
    game = games.get(room_id)
    if not game:
        emit('error', {'message': 'Игра не найдена'})
        return
    # Определяем какой цвет свободен
    if game['players']['white'] is None:
        color = 'white'
    elif game['players']['black'] is None:
        color = 'black'
    else:
        emit('error', {'message': 'Комната полна'})
        return
    game['players'][color] = request.sid
    game['names'][color] = name
    join_room(room_id)
    # Уведомляем обоих игроков о старте
    emit('game_start', {
        'board': game['board'],
        'current_turn': game['current_turn'],
        'your_color': color,
        'opponent_name': game['names']['white' if color == 'black' else 'black'],
        'evaluation_history': game['evaluation_history']
    }, room=request.sid)
    # Оповещаем другого игрока (если он уже есть)
    other_sid = game['players']['white'] if color == 'black' else game['players']['black']
    if other_sid:
        emit('opponent_joined', {
            'opponent_name': name
        }, room=other_sid)

@socketio.on('make_move')
def handle_make_move(data):
    room_id = data['room_id']
    from_pos = tuple(data['from'])
    to_pos = tuple(data['to'])
    game = games.get(room_id)
    if not game:
        emit('error', {'message': 'Игра не найдена'})
        return
    if game['winner']:
        emit('error', {'message': 'Игра уже закончена'})
        return
    # Определяем цвет игрока по socket.id
    player_color = None
    for col in ['white', 'black']:
        if game['players'][col] == request.sid:
            player_color = col
            break
    if not player_color or player_color != game['current_turn']:
        emit('error', {'message': 'Не ваш ход'})
        return
    legal_moves = get_all_moves(game['board'], player_color)
    move_found = None
    for mv in legal_moves:
        if mv['from'] == from_pos and mv['to'] == to_pos:
            move_found = mv
            break
    if not move_found:
        emit('error', {'message': 'Недопустимый ход'})
        return
    new_board = apply_move(game['board'], move_found)
    game['board'] = new_board
    new_eval = evaluate_board(new_board)
    game['evaluation_history'].append(new_eval)
    # Смена хода
    opponent = 'black' if player_color == 'white' else 'white'
    if not has_any_move(game['board'], opponent):
        game['winner'] = player_color
        winner_name = game['names'][player_color]
        # Рассылаем окончание игры
        socketio.emit('game_over', {
            'winner': winner_name,
            'evaluation_history': game['evaluation_history']
        }, room=room_id)
        return
    else:
        game['current_turn'] = opponent
        socketio.emit('move_made', {
            'board': game['board'],
            'current_turn': game['current_turn'],
            'evaluation_history': game['evaluation_history']
        }, room=room_id)

@socketio.on('disconnect')
def handle_disconnect():
    # Поиск игры, в которой был игрок, и уведомление другого
    for room_id, game in list(games.items()):
        if request.sid in [game['players']['white'], game['players']['black']]:
            other_sid = game['players']['white'] if game['players']['black'] == request.sid else game['players']['black']
            if other_sid:
                emit('opponent_disconnected', room=other_sid)
            del games[room_id]
            break

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
