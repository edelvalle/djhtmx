from django.shortcuts import render


def draft(request):
    return render(request, "testbed/draft_page.html")


def board(request):
    return render(request, "testbed/board_page.html", context={"title": "board"})
