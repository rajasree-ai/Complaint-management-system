from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, TextAreaField, SelectField, SubmitField, SelectMultipleField
from wtforms.validators import DataRequired, Email, EqualTo, Length, ValidationError
from database import db
from models import User, Department, StudentStaffAssignment


class RegistrationForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=2, max=20)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6)])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('password')])
    
    # Student-specific fields
    department = SelectField('Department', choices=[], validators=[DataRequired()])
    year = SelectField('Year', choices=[
        ('1st Year', '1st Year'),
        ('2nd Year', '2nd Year'),
        ('3rd Year', '3rd Year'),
        ('4th Year', '4th Year')
    ], validators=[DataRequired()])
    section = SelectField('Section', choices=[
        ('A', 'A'),
        ('B', 'B'),
        ('C', 'C')
    ], validators=[DataRequired()])

    # Allow student to enter roll number at registration
    roll_number = StringField('Roll Number', validators=[DataRequired(), Length(max=20)])

    phone = StringField('Phone Number', validators=[DataRequired(), Length(min=10, max=15)])
    parent_name = StringField('Parent/Guardian Name', validators=[DataRequired()])
    parent_phone = StringField('Parent/Guardian Phone', validators=[DataRequired(), Length(min=10, max=15)])
    address = TextAreaField('Address', validators=[DataRequired()])
    
    submit = SubmitField('Register')
    
    def __init__(self, *args, **kwargs):
        super(RegistrationForm, self).__init__(*args, **kwargs)
        from models import Department
        departments = Department.query.order_by(Department.name).all()
        self.department.choices = [(d.name, d.name) for d in departments] if departments else [('', 'No departments available')]
    
    def validate_username(self, username):
        from models import User
        user = User.query.filter_by(username=username.data).first()
        if user:
            raise ValidationError('Username already exists.')
    
    def validate_email(self, email):
        from models import User
        user = User.query.filter_by(email=email.data).first()
        if user:
            raise ValidationError('Email already registered.')

    def validate_roll_number(self, roll_number):
        from models import User
        if not roll_number.data:
            raise ValidationError('Roll number is required for student accounts.')
        existing = User.query.filter_by(roll_number=roll_number.data.strip()).first()
        if existing:
            raise ValidationError('Roll number already exists.')

class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Login')


class ComplaintForm(FlaskForm):
    title = StringField('Title', validators=[DataRequired(), Length(max=100)])
    description = TextAreaField('Description', validators=[DataRequired()])
    category = SelectField('Category', choices=[
        ('academic', 'Academic'),
        ('administrative', 'Administrative'),
        ('facility', 'Facility'),
        ('harassment', 'Harassment'),
        ('technical', 'Technical'),
        ('other', 'Other')
    ], validators=[DataRequired()])
    priority = SelectField('Priority', choices=[
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High')
    ], default='medium')
    mentor_id = SelectField('Assign to Mentor (Optional)', choices=[], coerce=int, default=0)
    submit = SubmitField('Submit Complaint')
class CommentForm(FlaskForm):
    content = TextAreaField('Add a comment', validators=[DataRequired()])
    submit = SubmitField('Post Comment')


class UpdateComplaintForm(FlaskForm):
    status = SelectField('Status', choices=[
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('resolved', 'Resolved'),
        ('rejected', 'Rejected')
    ])
    assigned_to = SelectField('Assign To', choices=[], coerce=int)
    submit = SubmitField('Update')


class ForgotPasswordForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    submit = SubmitField('Send OTP')


class ResetPasswordForm(FlaskForm):
    otp = StringField('OTP', validators=[DataRequired(), Length(min=6, max=6)])
    new_password = PasswordField('New Password', validators=[DataRequired(), Length(min=6)])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('new_password')])
    submit = SubmitField('Reset Password')


class DepartmentForm(FlaskForm):
    name = StringField('Department Name', validators=[DataRequired()])
    hod_id = SelectField('Department Head (HOD)', choices=[], coerce=int)
    submit = SubmitField('Add Department')
    
    def __init__(self, *args, **kwargs):
        super(DepartmentForm, self).__init__(*args, **kwargs)
        users = User.query.all()
        self.hod_id.choices = [(0, 'Select HOD')] + [(u.id, f"{u.username} ({u.email})") for u in users]


class StudentStaffAssignmentForm(FlaskForm):
    staff_member = SelectField('Staff Member', choices=[], coerce=int, validators=[DataRequired()])
    students = SelectMultipleField('Students to Assign', choices=[], coerce=int, validators=[DataRequired()])
    notes = TextAreaField('Assignment Notes (Optional)', validators=[Length(max=500)])
    submit = SubmitField('Assign Students')
    
    def __init__(self, department_name, *args, **kwargs):
        super(StudentStaffAssignmentForm, self).__init__(*args, **kwargs)
        # Get staff members in the department
        staff_list = User.query.filter_by(department=department_name, role='staff').all()
        self.staff_member.choices = [(0, 'Select Staff Member')] + [(s.id, f"{s.username} ({s.email})") for s in staff_list]
        
        # Only show students who are not already assigned in this department.
        assigned_student_ids = db.session.query(StudentStaffAssignment.student_id).filter(
            StudentStaffAssignment.department == department_name
        ).distinct()
        student_list = User.query.filter_by(department=department_name, role='student').filter(
            ~User.id.in_(assigned_student_ids)
        ).all()
        self.students.choices = [(st.id, f"{st.roll_number or '-'} — {st.username} ({st.year} {st.section})") for st in student_list]


class RemoveStudentAssignmentForm(FlaskForm):
    submit = SubmitField('Remove Assignment')


class StaffStudentAssignmentForm(FlaskForm):
    students = SelectMultipleField('Students to Assign to Yourself', choices=[], coerce=int, validators=[DataRequired()])
    notes = TextAreaField('Assignment Notes (Optional)', validators=[Length(max=500)])
    submit = SubmitField('Assign Students to Me')
    
    def __init__(self, department_name, *args, **kwargs):
        super(StaffStudentAssignmentForm, self).__init__(*args, **kwargs)
        # Only show students who are not already assigned in this department.
        assigned_student_ids = db.session.query(StudentStaffAssignment.student_id).filter(
            StudentStaffAssignment.department == department_name
        ).distinct()
        student_list = User.query.filter_by(department=department_name, role='student').filter(
            ~User.id.in_(assigned_student_ids)
        ).all()
        self.students.choices = [(st.id, f"{st.roll_number or '-'} — {st.username} ({st.year} {st.section})") for st in student_list]


class UpdateProfileForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=2, max=20)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    department = SelectField('Department', choices=[], validators=[DataRequired()])
    year = SelectField('Year', choices=[
        ('1st Year', '1st Year'),
        ('2nd Year', '2nd Year'),
        ('3rd Year', '3rd Year'),
        ('4th Year', '4th Year')
    ], validators=[DataRequired()])
    section = SelectField('Section', choices=[
        ('A', 'A'),
        ('B', 'B'),
        ('C', 'C')
    ], validators=[DataRequired()])

    # Allow editing roll number on profile updates
    roll_number = StringField('Roll Number', validators=[Length(max=20)])

    phone = StringField('Phone Number', validators=[Length(min=10, max=15)])
    parent_name = StringField('Parent/Guardian Name')
    parent_phone = StringField('Parent/Guardian Phone', validators=[Length(min=10, max=15)])
    address = TextAreaField('Address')
    submit = SubmitField('Save Changes')

    def __init__(self, target_user=None, *args, **kwargs):
        super(UpdateProfileForm, self).__init__(*args, **kwargs)
        self.target_user = target_user
        departments = Department.query.order_by(Department.name).all()
        self.department.choices = [(d.name, d.name) for d in departments] if departments else [('', 'No departments available')]

    def validate_username(self, username):
        user = User.query.filter_by(username=username.data).first()
        if user and (not self.target_user or user.id != self.target_user.id):
            raise ValidationError('Username already exists.')

    def validate_email(self, email):
        user = User.query.filter_by(email=email.data).first()
        if user and (not self.target_user or user.id != self.target_user.id):
            raise ValidationError('Email already registered.')

    def validate_roll_number(self, roll_number):
        if not roll_number.data:
            # Allow empty roll_number for non-students but if provided check uniqueness
            return
        existing = User.query.filter_by(roll_number=roll_number.data.strip()).first()
        if existing and (not self.target_user or existing.id != self.target_user.id):
            raise ValidationError('Roll number already exists.')

    def __init__(self, target_user=None, *args, **kwargs):
        super(UpdateProfileForm, self).__init__(*args, **kwargs)
        self.target_user = target_user
        departments = Department.query.order_by(Department.name).all()
        self.department.choices = [(d.name, d.name) for d in departments] if departments else [('', 'No departments available')]

    def validate_username(self, username):
        user = User.query.filter_by(username=username.data).first()
        if user and (not self.target_user or user.id != self.target_user.id):
            raise ValidationError('Username already exists.')

    def validate_email(self, email):
        user = User.query.filter_by(email=email.data).first()
        if user and (not self.target_user or user.id != self.target_user.id):
            raise ValidationError('Email already registered.')
