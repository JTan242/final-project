import os

from flask import (
    Flask,
    jsonify,
    send_from_directory,
    request,
    render_template,
    redirect,
    url_for,
    make_response,
    session,
    flash
)
from flask_sqlalchemy import SQLAlchemy
import sqlalchemy
from sqlalchemy import text, create_engine
import psycopg2
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import datetime
import bleach   
import re

app = Flask(__name__)
app.config.from_object("project.config.Config")
db = SQLAlchemy(app)


db_url = "postgresql://postgres:pass@postgres:5432"
engine = sqlalchemy.create_engine(db_url, connect_args={'application_name': '__init__.py root()',})
connection = engine.connect()


def are_credentials_good(username, password):
    sql = sqlalchemy.sql.text('''
        SELECT username FROM users
        WHERE username = :username
        AND password = :password
        ;
        ''')

    cred = connection.execute(sql, {
        'username': username,
        'password': password
    })

    if cred.fetchone() is None:
        return False
    else:
        return True


def get_tweets(x):
    tweet_list = []
    query_text = sqlalchemy.sql.text(
        "SELECT users.username, tweets.text, tweets.created_at "
        "FROM tweets "
        "JOIN users ON users.id_users = tweets.id_users "
        "ORDER BY tweets.created_at DESC "
        "LIMIT 20 OFFSET :offset"
    )
    page = connection.execute(query_text, {'offset': (x - 1) * 20})
    for row in page.fetchall():
        tweet_list.append({
            'username': row[0],
            'text': row[1],
            'created_at': row[2],
              
        })
    return tweet_list


def search_helper(query, page_num):
    messages = []
    offset = (page_num - 1) * 20

    sql = sqlalchemy.sql.text("""
        SELECT id_tweets, text, created_at, id_users
        FROM tweets
        WHERE to_tsvector(text) @@ to_tsquery(:query)
        ORDER BY ts_rank(to_tsvector(text), to_tsquery(:query)) DESC,
        created_at DESC
        LIMIT 20 OFFSET :offset;
    """)

    res = connection.execute(sql, {'query': ' & '.join(query.split()), 'offset': offset})

    for row in res.fetchall():
        user_sql = sqlalchemy.sql.text("""
            SELECT username
            FROM users
            WHERE id_users = :id_users;
        """)
        user_res = connection.execute(user_sql, {'id_users': row[3]})
        user_row = user_res.fetchone()

        # Highlight the message
        highlighted_text = highlight_query(row[1], query)

        messages.append({
            'username': user_row[0],
            'text': highlighted_text,
            'created_at': row[2]
        })

    return messages


def highlight_query(text, query):
    # Create a regular expression pattern for the query
    pattern = re.compile(rf'({re.escape(query)})', re.IGNORECASE)

    # Split the text into parts based on the query matches
    parts = pattern.split(text)

    # Create a list to store the parts along with their highlight status
    text_parts = [{'text': part, 'highlighted': False} for part in parts]

    # Iterate over the text parts and mark the ones that match the query as highlighted
    for part in text_parts:
        if pattern.search(part['text']):
            part['highlighted'] = True

    return text_parts


@app.route("/")
def root():
    username=request.cookies.get('username')
    password=request.cookies.get('password')
    good_credentials=are_credentials_good(username, password)
    if good_credentials:
        logged_in=True
    else:
        logged_in=False

    page_num = int(request.args.get('page', 1))
    tweet_list = get_tweets(page_num)

    return render_template('root.html', logged_in=logged_in, page_num=page_num, username=username,tweet_list=tweet_list)


@app.route('/login', methods=['GET', 'POST'])
def login():

    username=request.cookies.get('username')
    password=request.cookies.get('password')

    good_credentials=are_credentials_good(username, password)
    if good_credentials:
        logged_in=True
    else:
        logged_in=False
    print('logged-in=',logged_in)

    if logged_in:
        return redirect('/')

    username=request.form.get('username')
    password=request.form.get('password')

    good_credentials=are_credentials_good(username, password)
    print('good_credentials=',good_credentials)

    # first time we visited, no form submission
    if username is None:
        return render_template('login.html', bad_credentials=False)

    # they submitted a form--we're on the POST method
    else:
        if not good_credentials:
            return render_template('login.html', bad_credentials=True)
        else:
            #create a cookie that contains the username/password info
            # set cookie
            response = make_response(redirect('/'))
            response.set_cookie('username',username)
            response.set_cookie('password',password)
            return response


@app.route("/logout")
def logout():
    response = make_response(render_template('logout.html'))
    response.delete_cookie('username')
    response.delete_cookie('password')
    return response


@app.route('/create_account', methods=['GET', 'POST'])
def create_account():
    if request.method == 'POST':
        new_username = request.form.get('new_username')
        new_password = request.form.get('new_password')
        new_password2 = request.form.get('new_password2')

        if not all([new_username, new_password, new_password2]):
            return render_template('create_account.html', one_blank=True)
        elif new_password != new_password2:
            return render_template('create_account.html', not_matching=True)

        try:
            with connection.begin() as trans:
                sql = sqlalchemy.sql.text('''
                    INSERT INTO users (username, password)
                    VALUES (:username, :password)
                    ''')

                cred = connection.execute(sql, {
                    'username': new_username,
                    'password': new_password
                })

            # Set cookies for the new user
            response = make_response(redirect('/'))
            response.set_cookie('username', new_username)
            response.set_cookie('password', new_password)
            return response
        except sqlalchemy.exc.IntegrityError:
            return render_template('create_account.html', already_exists=True)
    return render_template('create_account.html')


@app.route('/create_message', methods=['GET', 'POST'])
def create_message():
    username = request.cookies.get('username')
    password = request.cookies.get('password')

    if not (username and password):
        return redirect('/')

    good_credentials = are_credentials_good(username, password)
    logged_in = True if good_credentials else False

    if request.method == 'GET':
        return render_template('create_message.html', logged_in=logged_in)

    message = request.form.get('message')

    if not message:
        return render_template('create_message.html', invalid_message=True, logged_in=logged_in)

    try:
        with connection.begin() as trans:
            user_query = sqlalchemy.sql.text('''
                SELECT id_users FROM users
                WHERE username = :username AND password = :password
            ''')
            res = connection.execute(user_query, {'username': username, 'password': password})
            user_id = res.scalar()  # Fetch the result directly

            insert_query = sqlalchemy.sql.text('''
                INSERT INTO tweets (id_users, text, created_at)
                VALUES (:id_users, :text, :created_at)
            ''')
            created_at = datetime.datetime.now()
            message_data = str(created_at).split('.')[0]  # Formatting the datetime
            connection.execute(insert_query, {'id_users': user_id, 'text': message, 'created_at': message_data})
    except sqlalchemy.exc.SQLAlchemyError as e:
        print(e)
        return render_template('create_message.html', error=True, logged_in=logged_in)
    else:
        return render_template('create_message.html', message_sent=True, logged_in=logged_in)


@app.route("/search", methods=['GET', 'POST'])
def search():
    username = request.cookies.get('username')
    password = request.cookies.get('password')
    good_credentials = are_credentials_good(username, password)

    if good_credentials:
        logged_in = True
    else:
        logged_in = False
    print('logged-in=', logged_in)

    page_num = int(request.args.get('page', 1))

    query = request.form.get('query')

    if query:
        messages = search_helper(query, page_num)
    else:
        messages = get_tweets(page_num)

    response = make_response(render_template('search.html', messages=messages, logged_in=logged_in,
                                             username=username, page_num=page_num, query=query))

    return response

   
@app.route("/static/<path:filename>")
def staticfiles(filename):
    return send_from_directory(app.config["STATIC_FOLDER"], filename)


@app.route("/media/<path:filename>")
def mediafiles(filename):
    return send_from_directory(app.config["MEDIA_FOLDER"], filename)


@app.route("/upload", methods=["GET", "POST"])
def upload_file():
    if request.method == "POST":
        file = request.files["file"]
        filename = secure_filename(file.filename)
        file.save(os.path.join(app.config["MEDIA_FOLDER"], filename))
    return """
    <!doctype html>
    <title>upload new File</title>
    <form action="" method=post enctype=multipart/form-data>
      <p><input type=file name=file><input type=submit value=Upload>
    </form>
    """

if __name__ == "__main__":
    app.run(debug=True)
