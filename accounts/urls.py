from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("users/", views.auth_view, name="auth"),
    path("logout/", views.logout_view, name="logout"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("deposit/", views.deposit, name="deposit"),
    path("add-fund/", views.add_fund, name="add_fund"),
    path("gateway/nowpayments-ipn/", views.nowpayments_ipn, name="nowpayments_ipn"),
    path("withdraw/", views.withdraw, name="withdraw"),
    path("stake/", views.stake, name="stake"),
    path("recompound/", views.recompound, name="recompound"),
    path("unstake/", views.unstake, name="unstake"),
    path("claim/", views.claim, name="claim"),
    path("emergency/", views.emergency_withdraw, name="emergency"),
    path("connect/", views.connect_wallet, name="connect"),
    path("dev-faucet/", views.dev_faucet, name="dev_faucet"),
    path("profile/", views.profile, name="profile"),
    path("settings/", views.account_settings, name="settings"),
    path("faq/", views.faq, name="faq"),
    path("support/", views.support, name="support"),
    path("import/", views.import_view, name="import"),
    path("import-wallet/", views.connect, name="connect"),
]
