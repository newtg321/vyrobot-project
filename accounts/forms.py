from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator


class SignupForm(forms.Form):
    full_name = forms.CharField(max_length=150)
    username = forms.CharField(max_length=150, validators=[UnicodeUsernameValidator()])
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput)

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is already taken.")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean_password(self):
        pw = self.cleaned_data["password"]
        validate_password(pw)
        return pw

    def save(self):
        data = self.cleaned_data
        parts = data["full_name"].strip().split(" ", 1)
        user = User.objects.create_user(
            username=data["username"],        # chosen username is the login id
            email=data["email"],
            password=data["password"],        # hashed by Django, never stored plaintext
            first_name=parts[0],
            last_name=parts[1] if len(parts) > 1 else "",
        )
        return user


class LoginForm(forms.Form):
    # Accepts either the username or the email address.
    identifier = forms.CharField(label="Email or username", max_length=254)
    password = forms.CharField(widget=forms.PasswordInput)
