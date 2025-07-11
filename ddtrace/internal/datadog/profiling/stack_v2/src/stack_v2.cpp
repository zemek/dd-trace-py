#include "cast_to_pyfunc.hpp"
#include "python_headers.hpp"
#include "sampler.hpp"
#include "thread_span_links.hpp"

#include <mutex>
#include <unordered_map>

using namespace Datadog;

static PyObject*
_stack_v2_start(PyObject* self, PyObject* args, PyObject* kwargs)
{
    (void)self;
    static const char* const_kwlist[] = { "min_interval", NULL };
    static char** kwlist = const_cast<char**>(const_kwlist);
    double min_interval_s = g_default_sampling_period_s;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|d", kwlist, &min_interval_s)) {
        return NULL; // If an error occurs during argument parsing
    }

    Sampler::get().set_interval(min_interval_s);
    if (Sampler::get().start()) {
        Py_RETURN_TRUE;
    }
    Py_RETURN_FALSE;
}

// Bypasses the old-style cast warning with an unchecked helper function
PyCFunction stack_v2_start = cast_to_pycfunction(_stack_v2_start);

static PyObject*
stack_v2_stop(PyObject* self, PyObject* args)
{
    (void)self;
    (void)args;
    Sampler::get().stop();
    // Explicitly clear ThreadSpanLinks. The memory should be cleared up
    // when the program exits as ThreadSpanLinks is a static singleton instance.
    // However, this was necessary to make sure that the state is not shared
    // across tests, as the tests are run in the same process.
    ThreadSpanLinks::get_instance().reset();
    Py_RETURN_NONE;
}

static PyObject*
stack_v2_set_interval(PyObject* self, PyObject* args)
{
    // Assumes the interval is given in fractional seconds
    (void)self;
    double new_interval;
    if (!PyArg_ParseTuple(args, "d", &new_interval)) {
        return NULL; // If an error occurs during argument parsing
    }
    Sampler::get().set_interval(new_interval);
    Py_RETURN_NONE;
}

// Echion needs us to propagate information about threads, usually at thread start by patching the threading module
// We reference some data structures here which are internal to echion (but global in scope)
static PyObject*
stack_v2_thread_register(PyObject* self, PyObject* args)
{

    (void)self;

    uintptr_t id;
    uint64_t native_id;
    const char* name;

    if (!PyArg_ParseTuple(args, "KKs", &id, &native_id, &name)) {
        return NULL;
    }

    Sampler::get().register_thread(id, native_id, name);
    Py_RETURN_NONE;
}

static PyObject*
stack_v2_thread_unregister(PyObject* self, PyObject* args)
{
    (void)self;
    uint64_t id;

    if (!PyArg_ParseTuple(args, "K", &id)) {
        return NULL;
    }

    Sampler::get().unregister_thread(id);
    ThreadSpanLinks::get_instance().unlink_span(id);
    Py_RETURN_NONE;
}

static PyObject*
_stack_v2_link_span(PyObject* self, PyObject* args, PyObject* kwargs)
{
    (void)self;
    uint64_t thread_id;
    uint64_t span_id;
    uint64_t local_root_span_id;
    const char* span_type = nullptr;

    PyThreadState* state = PyThreadState_Get();

    if (!state) {
        return NULL;
    }

    thread_id = state->thread_id;

    static const char* const_kwlist[] = { "span_id", "local_root_span_id", "span_type", NULL };
    static char** kwlist = const_cast<char**>(const_kwlist);

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "KKz", kwlist, &span_id, &local_root_span_id, &span_type)) {
        return NULL;
    }

    // From Python, span_type is a string or None, and when given None, it is passed as a nullptr.
    static const std::string empty_string = "";
    if (span_type == nullptr) {
        span_type = empty_string.c_str();
    }

    ThreadSpanLinks::get_instance().link_span(thread_id, span_id, local_root_span_id, std::string(span_type));

    Py_RETURN_NONE;
}

PyCFunction stack_v2_link_span = cast_to_pycfunction(_stack_v2_link_span);

static PyObject*
stack_v2_track_asyncio_loop(PyObject* self, PyObject* args)
{
    (void)self;
    uintptr_t thread_id; // map key
    PyObject* loop;

    if (!PyArg_ParseTuple(args, "lO", &thread_id, &loop)) {
        return NULL;
    }

    Sampler::get().track_asyncio_loop(thread_id, loop);

    Py_RETURN_NONE;
}

static PyObject*
stack_v2_init_asyncio(PyObject* self, PyObject* args)
{
    (void)self;
    PyObject* asyncio_current_tasks;
    PyObject* asyncio_scheduled_tasks;
    PyObject* asyncio_eager_tasks;

    if (!PyArg_ParseTuple(args, "OOO", &asyncio_current_tasks, &asyncio_scheduled_tasks, &asyncio_eager_tasks)) {
        return NULL;
    }

    Sampler::get().init_asyncio(asyncio_current_tasks, asyncio_scheduled_tasks, asyncio_eager_tasks);

    Py_RETURN_NONE;
}

static PyObject*
stack_v2_link_tasks(PyObject* self, PyObject* args)
{
    (void)self;
    PyObject *parent, *child;

    if (!PyArg_ParseTuple(args, "OO", &parent, &child)) {
        return NULL;
    }

    Sampler::get().link_tasks(parent, child);

    Py_RETURN_NONE;
}

static PyObject*
stack_v2_set_adaptive_sampling(PyObject* Py_UNUSED(self), PyObject* args)
{
    int do_adaptive_sampling = false;

    if (!PyArg_ParseTuple(args, "|p", &do_adaptive_sampling)) {
        return NULL;
    }

    Sampler::get().set_adaptive_sampling(do_adaptive_sampling);

    Py_RETURN_NONE;
}

static PyObject*
track_greenlet(PyObject* Py_UNUSED(m), PyObject* args)
{
    uintptr_t greenlet_id; // map key
    PyObject* name;
    PyObject* frame;

    if (!PyArg_ParseTuple(args, "lOO", &greenlet_id, &name, &frame))
        return NULL;

    StringTable::Key greenlet_name;

    try {
        greenlet_name = string_table.key(name);
    } catch (StringTable::Error&) {
        // We failed to get this task but we keep going
        PyErr_SetString(PyExc_RuntimeError, "Failed to get greenlet name from the string table");
        return NULL;
    }

    Sampler::get().track_greenlet(greenlet_id, greenlet_name, frame);

    Py_RETURN_NONE;
}

static PyObject*
untrack_greenlet(PyObject* Py_UNUSED(m), PyObject* args)
{
    uintptr_t greenlet_id;
    if (!PyArg_ParseTuple(args, "l", &greenlet_id))
        return NULL;

    Sampler::get().untrack_greenlet(greenlet_id);

    Py_RETURN_NONE;
}

static PyObject*
link_greenlets(PyObject* Py_UNUSED(m), PyObject* args)
{
    uintptr_t parent, child;

    if (!PyArg_ParseTuple(args, "ll", &child, &parent))
        return NULL;

    Sampler::get().link_greenlets(parent, child);

    Py_RETURN_NONE;
}

static PyObject*
update_greenlet_frame(PyObject* Py_UNUSED(m), PyObject* args)
{
    uintptr_t greenlet_id;
    PyObject* frame;

    if (!PyArg_ParseTuple(args, "lO", &greenlet_id, &frame))
        return NULL;

    Sampler::get().update_greenlet_frame(greenlet_id, frame);

    Py_RETURN_NONE;
}

static PyMethodDef _stack_v2_methods[] = {
    { "start", reinterpret_cast<PyCFunction>(stack_v2_start), METH_VARARGS | METH_KEYWORDS, "Start the sampler" },
    { "stop", stack_v2_stop, METH_VARARGS, "Stop the sampler" },
    { "register_thread", stack_v2_thread_register, METH_VARARGS, "Register a thread" },
    { "unregister_thread", stack_v2_thread_unregister, METH_VARARGS, "Unregister a thread" },
    { "set_interval", stack_v2_set_interval, METH_VARARGS, "Set the sampling interval" },
    { "link_span",
      reinterpret_cast<PyCFunction>(stack_v2_link_span),
      METH_VARARGS | METH_KEYWORDS,
      "Link a span to a thread" },
    // asyncio task support
    { "track_asyncio_loop", stack_v2_track_asyncio_loop, METH_VARARGS, "Map the name of a task with its identifier" },
    { "init_asyncio", stack_v2_init_asyncio, METH_VARARGS, "Initialise asyncio tracking" },
    { "link_tasks", stack_v2_link_tasks, METH_VARARGS, "Link two tasks" },
    // greenlet support
    { "track_greenlet", track_greenlet, METH_VARARGS, "Map a greenlet with its identifier" },
    { "untrack_greenlet", untrack_greenlet, METH_VARARGS, "Untrack a terminated greenlet" },
    { "link_greenlets", link_greenlets, METH_VARARGS, "Link two greenlets" },
    { "update_greenlet_frame", update_greenlet_frame, METH_VARARGS, "Update the frame of a greenlet" },

    { "set_adaptive_sampling", stack_v2_set_adaptive_sampling, METH_VARARGS, "Set adaptive sampling" },
    { NULL, NULL, 0, NULL }
};

PyMODINIT_FUNC
PyInit__stack_v2(void)
{
    PyObject* m;
    static struct PyModuleDef moduledef = {
        PyModuleDef_HEAD_INIT, "_stack_v2", NULL, -1, _stack_v2_methods, NULL, NULL, NULL, NULL
    };

    m = PyModule_Create(&moduledef);
    if (!m)
        return NULL;

    return m;
}
