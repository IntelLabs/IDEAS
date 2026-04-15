use std::fmt;
use std::ops::{Deref, DerefMut};
use std::ptr;
use std::sync::{
    LockResult, Mutex as StdMutex, MutexGuard as StdMutexGuard, PoisonError, TryLockError,
    TryLockResult,
};

#[repr(C)]
pub struct Mutex<T: Sized> {
    // Keep the payload first so it stays at offset 0 for C interop.
    pub value: T,
    inner: StdMutex<()>,
}

pub struct MutexGuard<'a, T: Sized> {
    _inner: StdMutexGuard<'a, ()>,
    value: &'a mut T,
}

impl<T> Mutex<T> {
    pub const fn new(value: T) -> Self {
        Self {
            value,
            inner: StdMutex::new(()),
        }
    }

    pub fn into_inner(self) -> LockResult<T> {
        let Self { inner, value } = self;

        match inner.into_inner() {
            Ok(()) => Ok(value),
            Err(_) => Err(PoisonError::new(value)),
        }
    }
}

impl<T: Sized> Mutex<T> {
    pub fn lock(&self) -> LockResult<MutexGuard<'_, T>> {
        match self.inner.lock() {
            Ok(inner) => Ok(self.guard_from_inner(inner)),
            Err(error) => Err(PoisonError::new(self.guard_from_inner(error.into_inner()))),
        }
    }

    pub fn try_lock(&self) -> TryLockResult<MutexGuard<'_, T>> {
        match self.inner.try_lock() {
            Ok(inner) => Ok(self.guard_from_inner(inner)),
            Err(TryLockError::WouldBlock) => Err(TryLockError::WouldBlock),
            Err(TryLockError::Poisoned(error)) => Err(TryLockError::Poisoned(PoisonError::new(
                self.guard_from_inner(error.into_inner()),
            ))),
        }
    }

    pub fn is_poisoned(&self) -> bool {
        self.inner.is_poisoned()
    }

    pub fn clear_poison(&self) {
        self.inner.clear_poison();
    }

    pub fn get_mut(&mut self) -> LockResult<&mut T> {
        match self.inner.get_mut() {
            Ok(()) => Ok(&mut self.value),
            Err(_) => Err(PoisonError::new(&mut self.value)),
        }
    }

    fn guard_from_inner<'a>(&'a self, inner: StdMutexGuard<'a, ()>) -> MutexGuard<'a, T> {
        MutexGuard {
            _inner: inner,
            value: unsafe { &mut *self.data_ptr() },
        }
    }

    pub fn data_ptr(&self) -> *mut T {
        ptr::addr_of!(self.value).cast_mut()
    }
}

impl<T> From<T> for Mutex<T> {
    fn from(value: T) -> Self {
        Self::new(value)
    }
}

impl<T: Default> Default for Mutex<T> {
    fn default() -> Self {
        Self::new(T::default())
    }
}

impl<T: fmt::Debug + Sized> fmt::Debug for Mutex<T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.try_lock() {
            Ok(guard) => f.debug_struct("Mutex").field("data", &&*guard).finish(),
            Err(TryLockError::Poisoned(error)) => {
                let guard = error.into_inner();
                f.debug_struct("Mutex")
                    .field("data", &&*guard)
                    .field("poisoned", &true)
                    .finish()
            }
            Err(TryLockError::WouldBlock) => f
                .debug_struct("Mutex")
                .field("data", &format_args!("<locked>"))
                .finish(),
        }
    }
}

impl<T: Sized> Deref for MutexGuard<'_, T> {
    type Target = T;

    fn deref(&self) -> &Self::Target {
        self.value
    }
}

impl<T: Sized> DerefMut for MutexGuard<'_, T> {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self.value
    }
}

impl<T: fmt::Debug + Sized> fmt::Debug for MutexGuard<'_, T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Debug::fmt(self.value, f)
    }
}

unsafe impl<T: Send + Sized> Send for Mutex<T> {}
unsafe impl<T: Send + Sized> Sync for Mutex<T> {}
