========
 djhtmx
========

Interactive UI Components for Django using htmx_.

Installation
============

Add ``djhtmx`` to your ``INSTALLED_APPS`` and add it to your `urls.py` as you
wish, you can use any path.  We recommend something like::

	from django.urls import path, include

	urlpatterns = [
		# ...
		path('__htmx/', include('djhtmx.urls')),
		# ...
	]


In your base template you need to load the necessary scripts to make this
work::

	{% load htmx %}

	<!doctype html>
	<html>
	  <head>
		{% htmx-headers %}
	  </head>
	</html>


Getting started
===============

The app looks for ``live`` python modules in your apps and imports them to
discover and and registers all HTMX components found there, but if you load
any module where you have components manually when Django boots up, that also
works.

::
   import typing as t
   from djhtmx import HTMXComponent

   class Counter(HTMXComponent):

       counter: int = 0
	   template_name: t.ClassVar[str] = 'counter.html'  # This could be a property

	   def mounted(self):
	       super().__init__(**kwargs)
		   self.counter = counter

	   def inc(self, amount: int = 1):
	       self.counter += amount

The ``counter.html`` could be::

	{% load htmx %}
	<div {% hx-tag %}>
	  {{ counter }}
	  <button {% on 'inc' %}>+</button>
	  <button {% on 'inc' amount=2 %}>+2</button>
	</div>


Now use the component in any of your html templates::

	{% load htmx %}

	Counter: <br/>
	{% htmx 'Counter' %}

	Counter with init value 3:<br/>
	{% htmx 'Counter' counter=3 %}


.. _htmx: https://htmx.org
