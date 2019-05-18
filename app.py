# Todo check if data is overwritten or space is left in tempfile
# add temporary folder creation code because that folder is temporary
# code warrants a comeback
from gevent import monkey
monkey.patch_all()
from flask import Flask, render_template, request, redirect
from flask import session, flash, url_for
from flask_mysqldb import MySQL
from werkzeug import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from boto3.session import Session
import face_recognition
from PIL import Image
from gevent import wsgi
import io
import os
import sys
import json
import urllib
import logging
import tempfile
import numpy as np

app = Flask(__name__)
db_url = os.environ['CLEARDB_DATABASE_URL'].split('//')
# db_url = os.environ['LOCAL_DATABASE_URL'].split('//')
aws_key = os.environ['ACCESS_KEY_S3']
aws_secret = os.environ['SECRET_ACCESS_S3']
region = os.environ['REGION']
bucket_name = os.environ['BUCKET_NAME']
aws_session = Session(aws_access_key_id=aws_key,
                      aws_secret_access_key=aws_secret, region_name=region)
s3 = aws_session.resource('s3')
ALLOWED_EXTENSIONS = set(['jpg'])
app.secret_key = os.environ['APP_SECRET']
app.config['MYSQL_USER'] = db_url[1].split(':')[0]
app.config['MYSQL_PASSWORD'] = db_url[1].split(':')[1].split('@')[0]
app.config['MYSQL_DB'] = db_url[1].split(':')[1].split('@')[1].split('/')[1].split('?')[0]
app.config['MYSQL_HOST'] = db_url[1].split(':')[1].split('@')[1].split('/')[0]
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # sets the maximum file size to 16MB
mysql = MySQL(app)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_user_credentials(emailid):
    conn = mysql.connection
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM usercred WHERE emailid = "' + emailid + '";')
    data = cursor.fetchall()
    return data

def add_user_credentials(emailid, password):
    conn = mysql.connection
    cursor = conn.cursor()
    cursor.execute('INSERT INTO usercred VALUES("{0}", "{1}", 0)'.format(emailid, password))
    conn.commit()

def update_name_change(emailid):
    conn = mysql.connection
    cursor = conn.cursor()
    cursor.execute('UPDATE usercred SET hasdb = 1 WHERE emailid = "'+ emailid + '";')
    conn.commit()

def insert_attendance(emailid, names, date_to_store):
    conn = mysql.connection
    cursor = conn.cursor()
    for name in names:
        cursor.execute('INSERT INTO studentattendance VALUES ("'+name+'", "'+date_to_store+'");')
    conn.commit()

def get_attendance():
    conn = mysql.connection
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM studentattendance')
    data = cursor.fetchall()
    return data

def clear_database():
    conn = mysql.connection
    cursor = conn.cursor()
    cursor.execute('TRUNCATE studentattendance')
    conn.commit()

def face_extraction(filepaths):
    ''' Extracts images from imgdirs and saves the extracted faces in a s3 storage '''
    ground_truth_image = face_recognition.load_image_file(filepaths[0])
    known_face_encodings = face_recognition.face_encodings(ground_truth_image)
    for index, face_location in enumerate(face_recognition.face_locations(ground_truth_image)):
        top, right, bottom, left = face_location
        pil_image = Image.fromarray(ground_truth_image[top:bottom, left:right])
        # students are ordered by number
        with tempfile.TemporaryFile() as tf:  # can we make the format more general depending on the file extension
            pil_image.save(fp=tf, format='jpeg')
            tf.seek(0)                        # need to seek to zero before reading the file data
            # the acl allows us to access the object publicly
            s3.Bucket(bucket_name).put_object(ACL='public-read', Body=tf.read(),
                                              Key=os.path.join(session['USER_STUDENTS'], str(index) + '.jpg'))
        # for each face, save the numpy encoding
        with tempfile.TemporaryFile() as tf:
            np.save(tf, known_face_encodings[index])
            tf.seek(0)
            s3.Bucket(bucket_name).put_object(Body=tf.read(),
                                              Key=os.path.join(session['FACE_ENCODINGS'], str(index) + '.npy'))
        print ('Saving ground truth operation complete!')
        # np.save(os.path.join(session['FACE_ENCODINGS'], str(index)), known_face_encodings[index])
        # print ('Saved cutout at ', os.path.join(session['USER_STUDENTS'], str(index)+'.jpg'))
    # adding additional faces assuming they are added in the same order
    # remove first image
    os.remove(filepaths[0])
    if (len(filepaths) > 1): # images other than the ground truth
        for current_index, image_dir in enumerate(filepaths[1:]):
            image = face_recognition.load_image_file(image_dir)
            unknown_face_encodings = face_recognition.face_encodings(image)
            face_locations = face_recognition.face_locations(image)
            # on finding unknown face, add it to the list of known faces as a new face
            for index, unknown_face_encoding in enumerate(unknown_face_encodings):
                if not True in face_recognition.compare_faces(known_face_encodings, unknown_face_encoding, 0.5):
                    known_face_encodings.append(unknown_face_encoding)  # add the new encodings, to the existing
                    top, right, bottom, left = face_locations[index]
                    pil_image = Image.fromarray(image[top:bottom, left:right])
                    with tempfile.TemporaryFile() as tf:
                        pil_image.save(fp=tf, format='jpeg')
                        tf.seek(0)
                        s3.Bucket(bucket_name).put_object(ACL='public-read', Body=tf.read(),
                                                          Key=os.path.join(session['USER_STUDENTS'],
                                                                           str(len(known_face_encodings)) + '.jpg'))
                    with tempfile.TemporaryFile() as tf:
                        np.save(tf, known_face_encodings[index])
                        tf.seek(0)
                        s3.Bucket(bucket_name).put_object(Body=tf.read(),
                                                          Key=os.path.join(session['FACE_ENCODINGS'],
                                                                           str(len(known_face_encodings)) + '.npy'))
        os.remove(filepaths[current_index + 1])

# to do, make this work for multiple images
def extract_attendance(image_dir):
    ''' Extracts images from imgdirs and gets the attendance.'''
    # global known_faces
    known_face_encodings, list_students = [], []
    search_prefix = session['FACE_ENCODINGS']
    face_object_list = s3.meta.client.list_objects(Bucket=bucket_name, Prefix=search_prefix)
    face_object_keys = [i['Key'] for i in face_object_list['Contents'] if i['Key'] != search_prefix]
    for i in face_object_keys:
        obj = s3.Bucket(bucket_name).Object(i)
        with io.BytesIO(obj.get()['Body'].read()) as temp_fileobj:
            temp_fileobj.seek(0)
            known_face_encodings.append(np.load(temp_fileobj))
            list_students.append(i.split('/')[-1][:-4])  # get rid of the '.npy' extension
    # known_face_encodings = known_faces
    print ('The list of students is', list_students)
    students_who_attended, unknown_students = [], []
    # adding additional faces assuming they are added in the same order
    # with tempfile.TemporaryFile() as tf:
    #    s3.Bucket(bucket_name).download_file(image_dir, tf)
    image = face_recognition.load_image_file(image_dir)
    unknown_face_encodings = face_recognition.face_encodings(image)
    face_locations = face_recognition.face_locations(image)
    os.remove(image_dir)  # get rid of the temp image
    # on finding unknown face, add it to the list of known faces as a new face
    for index, unknown_face_encoding in enumerate(unknown_face_encodings):
        temp = face_recognition.compare_faces(known_face_encodings, unknown_face_encoding, 0.5)  # lower tolerance is stricter
        if not True in temp:
            known_face_encodings.append(unknown_face_encoding)
            top, right, bottom, left = face_locations[index]
            pil_image = Image.fromarray(image[top:bottom, left:right])
            # the only numeric names
            with tempfile.TemporaryFile() as tf:
                pil_image.save(fp=tf, format='jpeg')
                tf.seek(0)
                s3.Bucket(bucket_name).put_object(ACL='public-read', Body=tf.read(),
                                                  Key=os.path.join(session['USER_STUDENTS'],
                                                                   str(len(known_face_encodings)) + '.jpg'))
            # pil_image.save(os.path.join(session['USER_STUDENTS'], str(len(known_face_encodings))+'.jpg'))
            with tempfile.TemporaryFile() as tf:
            #    tf.seek(0, 0)   # does this still leave data behind? Need to check
                np.save(tf, known_face_encodings[-1])
                s3.Bucket(bucket_name).put_object(Bytes=tf.read(),
                                                  Key=os.path.join(session['FACE_ENCODINGS'],
                                                                   str(len(known_face_encodings)) + '.npy'))
            # pil_image.save(os.path.join(session['USER_STUDENTS'], str(len(known_face_encodings))+'.jpg'))
            # np.save(os.path.join(session['FACE_ENCODINGS'], str(len(known_face_encodings))), known_face_encodings[-1])
            unknown_students.append(str(len(known_face_encodings)))  # save the filename of the unknown students
        else: # get the name of the student who attended
            # print ('temp is', temp)
            students_who_attended.append(list_students[temp.index(True)])
    print ('students who attended at face extraction site', students_who_attended)
    # this will be useful when there are multiple images upload of the class
    return (list(set(students_who_attended)), unknown_students)

@app.route('/', methods=['GET'])
def show_index():
    if request.method == 'GET':
        return render_template('homepage.html')

@app.route('/register', methods=['GET', 'POST'])
def show_register():
    if request.method == 'GET':
        return render_template('register.html')
    elif request.method == 'POST':
        username = request.form.get('username', 'None')
        password = request.form.get('password', 'None')
        hashed_password = generate_password_hash(password)
        add_user_credentials(username, hashed_password)
        return redirect(url_for('show_signin'))

@app.route('/signin', methods=['GET', 'POST'])
def show_signin():
    if request.method == 'GET':
        return render_template('signin.html')
    elif request.method == 'POST':  # if form data submitted
        data = request.get_json()
        emailid = data.get('username', 'None')
        password = data.get('password', 'None')
        hashed_password = generate_password_hash(password)
        data = get_user_credentials(emailid)
        if (len(data) > 0):
            # the temporary files are stored server side, while buckets are used to store more permanent items
            if (check_password_hash(str(data[0][1]), password)):
                sys.stdout.write('The password matches correctly\n')
                session['CURRENT_USER'] = emailid
                session['USER_TEMP'] = session['CURRENT_USER']  # do away with the tempstorage folder
                # session['USER_TEMP'] = os.path.join(session['USER_HOME'], 'tempstorage')
                session['USER_HOME'] = session['CURRENT_USER']
                session['USER_STUDENTS'] = os.path.join(session['USER_HOME'], 'studentfaces')
                session['FACE_ENCODINGS'] = os.path.join(session['USER_HOME'], 'encodings')
                return json.dumps({'success': 'Successful login'}), 200, {'contentType': 'application/json;charset=UTF-8'}
            else:
                sys.stdout.write("The password for the username doesn't match stored record\n")
            sys.stdout.flush()
        return json.dumps({'error': 'Failed to login'}), 400, {'contentType': 'application/json;charset=UTF-8'}

# this can be simplified by doing this through the / route but need to check if
# render template will work in that case
@app.route('/checkNewLogin')
def check_new_login():
    if session.get('CURRENT_USER'):
        if request.method == 'GET':
            data = get_user_credentials(session['CURRENT_USER'])
            if (len(data) > 0):
                if data[0][2] == 0:    # if the user did not already create a database
                    return render_template('uploadimages.html')
                else:
                    return redirect(url_for('show_user_home'))
            else:
                return redirect(url_for('show_index'))

@app.route('/addClassInfo', methods=['GET', 'POST'])
def add_class_info():
    ''' Upload photos to tempstorage directory and then extract face images and store in studentfaces directory '''
    if session.get('CURRENT_USER'):
        if request.method == 'POST':  # this would change on dropbox integration
            uploaded_files = request.files.getlist('file')
            filepaths = []  # the path to USER_HOME would automatically be created
            for f in uploaded_files:
                if f.filename == '' or not allowed_file(f.filename):
                    flash('No file selected or invalid file selected. Please try again.')
                    print ('Return to ', request)
                    return redirect(url_for('check_new_login'))
                else:       # save each file to the temporary path which is the user home (on the deployment instance)
                    filepath = os.path.join(session['USER_HOME'], secure_filename(f.filename))
                    # now make the directory with the name of the user that you created; this could be temporary
                    if not os.path.exists(session['USER_HOME']):
                        os.makedirs(session['USER_HOME'])
                    f.save(filepath)
                    filepaths.append(filepath)
            face_extraction(filepaths)  # pass the filepaths from where the images are going to processed
            return redirect(url_for('add_user_details'))

@app.route('/addUserDetails', methods=['GET', 'POST'])
def add_user_details():   # this page will be used to add the names of users to images
    if session.get('CURRENT_USER'):
        # global studentid_name
        # user_home_path = os.path.join(app.config['UPLOAD_FOLDER'], session['CURRENT_USER'])
        # student_faces_path = os.path.join(user_home_path, app.config['FACE_FOLDER'])
        # list_names = os.listdir(session['USER_STUDENTS'])
        # print ('session now is', session['USER_STUDENTS'])
        # list_students = set([j[0] for j in [i['Key'].split('/') for i in objects_to_search['Contents']]])
        # list_face_images = [os.path.join(session['USER_STUDENTS'], image) for image in list_students]
        # list_face_encodings = [os.path.join(session['FACE_ENCODINGS'], image[:-4]+'.npy') for image in list_students]
        search_prefix_1 = session['USER_STUDENTS'] + '/'
        objects_to_search = s3.meta.client.list_objects(Bucket=bucket_name, Prefix=search_prefix_1)
        list_face_images = [i['Key'] for i in objects_to_search['Contents'] if i['Key'] != search_prefix_1]
        list_face_names = [int(i.split('/')[-1][:-4]) for i in list_face_images]
        url_prefix = 'https://s3.amazonaws.com/' + bucket_name + '/'
        face_image_urls = [url_prefix + urllib.parse.quote(name) for name in list_face_images]
        search_prefix_2 = session['FACE_ENCODINGS'] + '/'
        objects_to_search = s3.meta.client.list_objects(Bucket=bucket_name, Prefix=search_prefix_2)
        list_face_encodings = [i['Key'] for i in objects_to_search['Contents'] if i['Key'] != search_prefix_2]
        # display_face_paths = ['static/classroomimages/'+session['CURRENT_USER']+'/studentfaces/'+i for i in list_names]
        # num_faces = len(list_face_images)
        if request.method == 'GET':
            return render_template('addusername.html', face_urls=enumerate(face_image_urls),
                                                       face_names=list_face_names,
                                                       currentuser=session['CURRENT_USER'])
        elif request.method == 'POST':
            # store the names given to the images
            # student_names = []
            for index, name in enumerate(list_face_names):
                student_name = request.form['image' + str(name)]
                if student_name == '':  # indicates deletion
                    s3.meta.client.delete_objects(Bucket=bucket_name,
                                                  Delete={'Objects': [{'Key': list_face_images[index]}]})
                    s3.meta.client.delete_objects(Bucket=bucket_name,
                                                  Delete={'Objects': [{'Key': list_face_encodings[index]}]})
                else:
                    # create a temporary object to download the file
                    obj = s3.Bucket(bucket_name).Object(list_face_images[index])
                    with io.BytesIO(obj.get()['Body'].read()) as temp_fileobj:
                        temp_fileobj.seek(0)
                        image_data = temp_fileobj.read()
                    # with tempfile.NamedTemporaryFile(mode='r+b') as tf:
                        # s3.Bucket(bucket_name).download_file(list_face_images[index], tf.name)
                        # tf.seek(0, 0)
                        s3.meta.client.delete_objects(Bucket=bucket_name,
                                                      Delete={'Objects': [{'Key': list_face_images[index]}]})
                        s3.Bucket(bucket_name).put_object(ACL='public-read', Body=image_data,
                                                          Key=os.path.join(session['USER_STUDENTS'],
                                                                           student_name + '.jpg'))
                        # tf.seek(0, 0)  # need to check this (if this works, change code behind)
                    # with tempfile.NamedTemporaryFile(mode='r+b') as tf:
                    obj = s3.Bucket(bucket_name).Object(list_face_encodings[index])
                    with io.BytesIO(obj.get()['Body'].read()) as temp_fileobj:
                        temp_fileobj.seek(0)
                        numpy_data = temp_fileobj.read()
                        # s3.Bucket(bucket_name).download_file(list_face_encodings[index], tf.name)
                        # tf.seek(0, 0)
                        s3.meta.client.delete_objects(Bucket=bucket_name,
                                                      Delete={'Objects': [{'Key': list_face_encodings[index]}]})
                        s3.Bucket(bucket_name).put_object(ACL='public-read', Body=numpy_data,
                                                          Key=os.path.join(session['FACE_ENCODINGS'],
                                                                           student_name + '.npy'))
                    # os.rename(list_face_images[index], os.path.join(session['USER_STUDENTS'], student_name+'.jpg'))
                    # os.rename(list_face_encodings[index], os.path.join(session['FACE_ENCODINGS'], student_name+'.npy'))
            update_name_change(session['CURRENT_USER'])
            return redirect(url_for('show_user_home'))
            # test above
            # for index in range(len(list_face_images)):
            #     student_name = (request.form['image'+str(index)])
            #     print ('current student is', student_name)
            #     real_index = list_face_names[index]
            #     if student_name == '':
            #         os.remove(list_face_images[real_index])
            #         os.remove(list_face_encodings[real_index])
            #     else:
            #         # studentid_name[index] = student_names[-1]
            #         print ('list_face_images list is currently', list_face_images[index])
            #         os.rename(list_face_images[real_index], os.path.join(session['USER_STUDENTS'], student_name+'.jpg'))
            #         os.rename(list_face_encodings[real_index], os.path.join(session['FACE_ENCODINGS'], student_name+'.npy'))
            # print ('Names of student:', student_names)
            # print ('current user is ', session['CURRENT_USER'])

@app.route('/showUserHome', methods=['GET', 'POST'])
def show_user_home():
    if session.get('CURRENT_USER'):
        search_prefix_1 = session['USER_STUDENTS'] + '/'
        objects_to_search = s3.meta.client.list_objects(Bucket=bucket_name, Prefix=search_prefix_1)
        list_face_images = [i['Key'] for i in objects_to_search['Contents'] if i['Key'] != search_prefix_1]
        list_face_names = [i.split('/')[-1][:-4] for i in list_face_images]
        url_prefix = 'https://s3.amazonaws.com/' + bucket_name + '/'
        face_image_urls = [url_prefix + urllib.parse.quote(name) for name in list_face_images]
        # list_face_images = [os.path.join(session['USER_STUDENTS'], image) for image in os.listdir(session['USER_STUDENTS'])]
        # list_face_names = [os.path.basename(i)[:-4] for i in list_face_images]    # now each image should have a proper name
        attendance_data = get_attendance()
        # print (attendance_data, 'is the attendance')
        # display_face_paths = ['static/classroomimages/'+session['CURRENT_USER'] + '/studentfaces/'+i for i in list_names]
        if request.method == 'GET':
            return render_template('userhome.html', face_paths=enumerate(face_image_urls),
                                                    names=list_face_names,
                                                    currentuser=session['CURRENT_USER'])
        elif request.method == 'POST':
            if request.form.get('uploadattendance'):  # store an attendance sheet
                return redirect(url_for('add_attendance'))
            elif request.form.get('viewattendancestats'):
                if not attendance_data:
                    print ('No attendance data')
                    flash('You have not uploaded any attendance data. Please upload data.')
                    return redirect(url_for('show_user_home'))
                else:
                    print ('attendance data is ', attendance_data)
                    monthly_attendance = [0]*12
                    student_attendance_list = []
                    student_attendance = {}
                    for name in list_face_names:
                        student_attendance[name] = 0
                    for data_row in attendance_data:
                        monthly_attendance[int(data_row[1].split('-')[1])] += 1
                        student_attendance[data_row[0]] += 1
                    for name in list_face_names:
                        student_attendance_list.append(student_attendance[name])
                    url_prefix = 'https://s3.amazonaws.com/' + bucket_name + '/' + session['CURRENT_USER'] + '/studentfaces/'
                    known_face_image_urls = [url_prefix + urllib.parse.quote(name) + '.jpg' for name in list_face_names]
                    return render_template('attendancestats.html',
                                            face_paths=enumerate(known_face_image_urls),
                                            names=list_face_names,
                                            graph_data=monthly_attendance,
                                            student_attendance=student_attendance_list)
            elif request.form.get('cleardb'):
                if not attendance_data:
                    print ('No attendance data')
                    flash('You have not uploaded any attendance data. Please upload data.')
                    return redirect(url_for('show_user_home'))
                else:
                    print ('Clearing database')
                    flash('Successfully cleared the database')
                    clear_database()
                    return redirect(url_for('show_user_home'))
            elif request.form.get('logout'):
                print ('Signout was clicked')
                session.pop('CURRENT_USER')
                session.pop('USER_HOME')
                session.pop('USER_TEMP')
                session.pop('USER_STUDENTS')
                session.pop('FACE_ENCODINGS')
                return redirect(url_for('show_index'))

# need to convert the single file functionality to mulitple files
@app.route('/addAttendanceData', methods=['GET', 'POST'])
def add_attendance():
    if session.get('CURRENT_USER'):
        if request.method == 'GET':
            return render_template('attendanceupload.html',
                                    currentuser=session['CURRENT_USER'])
        if request.method == 'POST':
            # global attendance
            f = request.files.get("file")
            # user_home_path = os.path.join(app.config['UPLOAD_FOLDER'], session['CURRENT_USER'])
            # temp_upload_dir = os.path.join(user_home_path, 'tempstorage')
            # student_faces_path = os.path.join(user_home_path, app.config['FACE_FOLDER'])
            print ('file being uploaded for attendance is ', f.filename)
            if f.filename == '' or not allowed_file(f.filename):
                flash('No file selected or incorrect extension. Please try again.')
                print ('No file apparently')
                return redirect(url_for('add_attendance'))
            else:  # save on temporary storage on disk
                filepath = os.path.join(session['USER_HOME'], secure_filename(f.filename))
                f.save(filepath)
            # user_home_path = os.path.join(app.config['UPLOAD_FOLDER'], session['CURRENT_USER'])
            # student_faces_path = os.path.join(user_home_path, app.config['FACE_FOLDER'])
                students_who_attended, unknown_students = extract_attendance(filepath)
                session['students_who_attended'] = students_who_attended
                session['unknown_students'] = unknown_students
                return redirect(url_for('verify_attendance'))

@app.route('/verifyAttendanceData', methods=['GET', 'POST'])
def verify_attendance():
    if session.get('CURRENT_USER'):
        # global studentid_name, attendance
        # user_home_path = os.path.join(app.config['UPLOAD_FOLDER'], session['CURRENT_USER'])
        # student_faces_path = os.path.join(user_home_path, app.config['FACE_FOLDER'])
        # list_known_names = [i for i in os.listdir(student_faces_path) if studentid_name.get(int(i[:-4], '') != '')]
        # list_unknown_names = [i for i in os.listdir(student_faces_path) if studentid_name.get(int(i[:-4], '') == '')]
        students_who_attended = session['students_who_attended']
        unknown_students = session['unknown_students']
        print ('students who attended are', students_who_attended)
        print ('unknown students are ', unknown_students)
        # display_knownface_paths = ['static/classroomimages/'+session['CURRENT_USER']+'/studentfaces/'+i for i in list_known_names]
        # display_unknownface_paths = ['static/classroomimages/'+session['CURRENT_USER']+'/studentfaces/'+i for i in list_unknown_names]
        # list_unknownface_images = [os.path.abspath(i) for i in display_unknownface_paths]
        # dir_contents = os.listdir(session['USER_STUDENTS'])
        ## spagetti?
        search_prefix_1 = session['USER_STUDENTS'] + '/'
        objects_to_search = s3.meta.client.list_objects(Bucket=bucket_name, Prefix=search_prefix_1)
        dir_contents = [i['Key'] for i in objects_to_search['Contents'] if i['Key'] != search_prefix_1]
        # list_face_names = [int(i.split('/')[-1][:-4]) for i in list_face_images]
        # print ('The dir contents are', dir_contents)
        # known_student_paths = [os.path.join(session['USER_STUDENTS'], i) for i in dir_contents if i[:-4] in students_who_attended]
        known_student_paths = [i for i in dir_contents if i.split('/')[-1][:-4] in students_who_attended]
        known_student_names = [i.split('/')[-1][:-4] for i in known_student_paths]
        # known_student_names = [os.path.basename(i)[:-4] for i in known_student_paths]
        # these values would be the numeric values
        unknown_student_paths = [i for i in dir_contents if i.split('/')[-1][:-4] in unknown_students]
        # unknown_student_paths = [os.path.join(session['USER_STUDENTS'], i) for i in dir_contents if i[:-4] in unknown_students]
        # unknown_numpy_files = []
        search_prefix_2 = session['FACE_ENCODINGS'] + '/'
        objects_to_search = s3.meta.client.list_objects(Bucket=bucket_name, Prefix=search_prefix_2)
        dir_contents = [i['Key'] for i in objects_to_search['Contents'] if i['Key'] != search_prefix_2]
        unknown_numpy_files = [i for i in dir_contents if i.split('/')[-1][:-4] in unknown_students]
        # print ('Unknown paths are', unknown_student_paths)
        if request.method == 'GET':
            url_prefix = 'https://s3.amazonaws.com/' + bucket_name + '/' + session['CURRENT_USER'] + '/studentfaces/'
            known_face_image_urls = [url_prefix + urllib.parse.quote(name) + '.jpg' for name in known_student_names]
            unknown_face_image_urls = [url_prefix + urllib.parse.quote(name) + '.jpg' for name in unknown_students]
            if (unknown_student_paths):
                return render_template('attendanceinput.html', known_face_paths=enumerate(known_face_image_urls),
                                names=known_student_names, known_flag=False,
                                unknown_face_paths=enumerate(unknown_face_image_urls),
                                currentstudent=session['CURRENT_USER'])
            else:
                return render_template('attendanceinput.html', known_face_paths=enumerate(known_face_image_urls), names=known_student_names,
                unknown_face_paths=enumerate(unknown_face_image_urls), known_flag=True, currentstudent=session['CURRENT_USER'])
        elif request.method == 'POST':
            date_to_store = request.form.get('datefield')
            if not date_to_store:
                flash('Please add a date for storing attendance')
                return redirect(url_for('verify_attendance'))
            else:
                student_names, to_remove = [], []
                for index in range(len(unknown_student_paths)):
                    student_name = request.form['image'+str(index)]
                    if student_name == '':
                        print ('removing student', unknown_student_paths[index])
                        s3.meta.client.delete_objects(Bucket=bucket_name,
                                                      Delete={'Objects': [{'Key': unknown_student_paths[index]}]})
                        s3.meta.client.delete_objects(Bucket=bucket_name,
                                                      Delete={'Objects': [{'Key': unknown_numpy_files[index]}]})
                        # os.remove(unknown_student_paths[index])
                        # os.remove(unknown_numpy_files[index])
                        # to_remove.append(index)
                    else:
                        # print ('renaming with student name ', student_name, 'for ', unknown_student_paths[index])
                        obj = s3.Bucket(bucket_name).Object(os.path.join(session['USER_STUDENTS'],
                                                                          student_name + '.jpg'))
                        with io.BytesIO(obj.get()['Body'].read()) as temp_fileobj:
                            temp_fileobj.seek(0)
                            s3.meta.client.delete_objects(Bucket=bucket_name,
                                                          Delete={'Objects': [{'Key': unknown_student_paths[index]}]})
                            s3.Bucket(bucket_name).put_object(ACL='public-read', Body=temp_fileobj.read(),
                                                              Key=os.path.join(session['USER_STUDENTS'],
                                                              student_name + '.jpg'))

                        obj = s3.Bucket(bucket_name).Object(os.path.join(session['FACE_ENCODINGS'],
                                                                          student_name + '.npy'))
                        with io.BytesIO(obj.get()['Body'].read()) as temp_fileobj:
                            temp_fileobj.seek(0)
                            s3.meta.client.delete_objects(Bucket=bucket_name,
                                                          Delete={'Objects': [{'Key': unknown_numpy_files[index]}]})
                            s3.Bucket(bucket_name).put_object(ACL='public-read', Body=temp_fileobj.read(),
                                                              Key=os.path.join(session['FACE_ENCODINGS'],
                                                              student_name + '.npy'))
                insert_attendance(session['CURRENT_USER'], students_who_attended, date_to_store)
                return redirect(url_for('show_user_home'))

if __name__ == '__main__':
    # app.run(debug=True)
    port = int(os.environ.get('PORT', 5000))
    http_server = wsgi.WSGIServer(('', port), app)
    http_server.serve_forever()
